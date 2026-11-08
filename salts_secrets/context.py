from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .home import data_dir
from .tiers import CONTEXT_FILE_NAMES

ENTRY_DELIMITER = "\n§\n"  # tools/memory_tool_store.py
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".mdc"}
DATA_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".csv", ".toml", ".log", ".html", ".xml"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", ".mypy_cache", ".pytest_cache"}
SKIP_FILE_SUFFIXES = (".lock", ".pid", ".sock", "-wal", "-shm", "-journal", ".pyc")
# HERMES_HOME paths that hold config, secrets, caches or runtime state rather than the owner's content.
HOME_EXCLUDE = {
    ".env",
    "auth.json",
    "config.yaml",
    "cache",
    "plugins",
    "plugin-data",
    "profiles",
    "runtime",
    "installs",
    "state",
    "source-checks",
    "plugin-update-checks",
    "platforms",
    "pairing",
    "checkpoints",
    "sandboxes",
    "bin",
    "web_dist",
    "backups/config",
    "gateway_state.json",
    "gateway-starts.log",
    "install_id",
    "models_dev_cache.json",
    "models_dev_cache.etag",
    ".skills_prompt_snapshot.json",
    "runs_idempotency.db",
    "skills/.bundled_manifest",
    "cron/ticker_heartbeat",
    "cron/ticker_last_success",
}
MAX_FILE_BYTES = 1_000_000
MAX_FILES = 3000
ALLOWLIST_FILE = "public_allowlist.json"
PROJECTION_FILE = "public_context.json"
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
# Parsed items per file from the previous scan, reused while the file (and its SQLite WAL) is unchanged.
_MEMO: dict[tuple, tuple[tuple, list]] = {}


def display_path(path: str | Path) -> str:
    """Path as shown to the owner: ``<HERMES_HOME>/…`` inside the Hermes home, ``~/…`` inside the user's home."""
    from .home import current

    text = str(path)
    for root, label in ((str(current().resolve()), "<HERMES_HOME>"), (str(Path.home()), "~")):
        if text == root or text.startswith(root + os.sep):
            return label + text[len(root) :]
    return text


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:24]


@dataclass
class Item:
    id: str
    kind: str  # memory | user | soul | workspace | skill | session | provider | plugin
    group: str  # dashboard grouping label
    source: str  # path relative to its root, or a descriptor
    title: str
    text: str
    fingerprint: str
    publishable: bool = True
    note: str = ""
    path: str = ""  # absolute path of the backing file, "" for non-file sources
    meta: dict = field(default_factory=dict)

    def to_dict(self, with_text: bool = True) -> dict:
        d = asdict(self)
        d["path"] = display_path(self.path) if self.path else ""
        if not with_text:
            d.pop("text")
        return d


def _item(kind: str, group: str, source: str, locator: str, title: str, text: str, **kw) -> Item:
    fp = fingerprint(text)
    item_id = hashlib.sha256(f"{kind}\0{source}\0{locator}".encode()).hexdigest()[:20]
    return Item(id=item_id, kind=kind, group=group, source=source, title=title, text=text.strip(), fingerprint=fp, **kw)


def _read(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _memory_items(home: Path) -> list[Item]:
    items = []
    for name, kind, group in (("MEMORY.md", "memory", "Memory · MEMORY.md"), ("USER.md", "user", "Memory · USER.md")):
        raw = _read(home / "memories" / name)
        if not raw:
            continue
        for entry in (e.strip() for e in raw.split(ENTRY_DELIMITER)):
            if entry:
                # Entries have no stable name; their content is their identity (an edited entry is a new one).
                first = entry.splitlines()[0]
                items.append(
                    _item(
                        kind,
                        group,
                        f"memories/{name}",
                        fingerprint(entry),
                        first[:80],
                        entry,
                        path=str((home / "memories" / name).resolve()),
                    )
                )
    return items


NOTES_FILE = "notes.json"


def _note_items(home: Path) -> list[Item]:
    """Notes the owner wrote for guests from chat (``/salt``); private until approved like anything else."""
    path = data_dir(home) / NOTES_FILE
    try:
        notes = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [_item("note", "Notes for guests", f"plugin-data/salts-secrets/{NOTES_FILE}", n["id"],
                  f"Note {n['id']}: {n['text'].splitlines()[0][:60]}", n["text"], path=str(path.resolve()))
            for n in notes if isinstance(n, dict) and n.get("id") and str(n.get("text", "")).strip()]


def split_markdown(text: str) -> list[tuple[str, str]]:
    """``[(heading, body)]`` split at #/##/### headings; text before the first heading is "(preamble)"."""
    sections: list[tuple[str, list[str]]] = [("(preamble)", [])]
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            sections.append((m.group(2), [line]))
        else:
            sections[-1][1].append(line)
    out, seen = [], {}
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        seen[heading] = seen.get(heading, 0) + 1
        out.append((heading if seen[heading] == 1 else f"{heading} ({seen[heading]})", body))
    return out


def _doc_items(kind: str, group: str, rel: str, text: str, path: Path) -> list[Item]:
    abs_path = str(path.resolve())
    if Path(rel).suffix.lower() in {".md", ".markdown", ".mdc"}:
        parts = split_markdown(text)
        if len(parts) > 1:
            return [_item(kind, group, rel, h, f"{Path(rel).name} › {h}", body, path=abs_path) for h, body in parts]
    return [_item(kind, group, rel, "", Path(rel).name, text, path=abs_path)] if text.strip() else []


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _skip_file(name: str) -> bool:
    return name.endswith(SKIP_FILE_SUFFIXES) or name == ".DS_Store"


def _walk(root: Path, exclude: set[str], stop_dirs: set[Path], seen: set[Path]) -> Iterable[tuple[Path, str]]:
    """Yield ``(abs_path, rel)`` for every file under ``root``, so newly created paths show up automatically."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            full = here / d
            rel = full.relative_to(root).as_posix()
            if d in SKIP_DIRS or rel in exclude or full.resolve() in stop_dirs:
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(filenames):
            p = here / name
            rel = p.relative_to(root).as_posix()
            if _skip_file(name) or rel in exclude or p.resolve() in seen or not p.is_file():
                continue
            seen.add(p.resolve())
            count += 1
            if count > MAX_FILES:
                return
            yield p, rel


def _size(path: Path) -> str:
    n = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _session_items(db: Path, limit: int = 500, per_session: int = 30) -> list[Item]:
    try:
        con = _ro(db)
        cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        title = "title" if "title" in cols else ("display_name" if "display_name" in cols else "NULL")
        recent = "SELECT id FROM sessions ORDER BY started_at DESC LIMIT ?"
        rows = con.execute(
            f"SELECT id, {title}, source, started_at, message_count FROM sessions ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        msgs: dict[str, list[str]] = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='messages'").fetchone():
            for sid, role, content in con.execute(
                f"SELECT session_id, role, substr(content, 1, 400) FROM messages WHERE session_id IN ({recent}) "
                "AND role IN ('user','assistant') AND content IS NOT NULL AND content != '' ORDER BY id",
                (limit,),
            ):
                bucket = msgs.setdefault(sid, [])
                if len(bucket) < per_session:
                    bucket.append(f"[{role}] {content}")
        con.close()
    except sqlite3.Error:
        return []
    items = []
    for sid, t, src, started, n in rows:
        when = time.strftime("%Y-%m-%d", time.localtime(started or 0))
        label = f"{when} · {t or '(untitled)'} · {src} · {n or 0} messages"
        body = "\n\n".join(msgs.get(sid, [])) or label
        items.append(
            _item(
                "session",
                "Sessions · state.db",
                f"state.db/{sid}",
                "",
                label,
                body,
                publishable=False,
                note="sessions cannot be published in v1 (private and public content are mixed)",
                path=str(db.resolve()),
            )
        )
    return items


def _kanban_items(db: Path) -> list[Item]:
    try:
        con = _ro(db)
        tasks = con.execute("SELECT id, title, body, status FROM tasks ORDER BY created_at DESC").fetchall()
        comments: dict[str, list[str]] = {}
        for tid, author, body in con.execute("SELECT task_id, author, body FROM task_comments ORDER BY id"):
            comments.setdefault(tid, []).append(f"[{author}] {body}")
        con.close()
    except sqlite3.Error:
        return _database_items(db, "kanban.db")
    return [
        _item(
            "kanban",
            "Kanban · kanban.db",
            f"kanban.db/{tid}",
            "",
            f"{title} · {st}",
            "\n\n".join([title, body or "", *comments.get(tid, [])]),
            publishable=False,
            note="task records cannot be published",
            path=str(db.resolve()),
        )
        for tid, title, body, st in tasks
    ]


def _strings(node, out: list[str], budget: int = 4000) -> None:
    if sum(map(len, out)) > budget:
        return
    if isinstance(node, str):
        if node.strip():
            out.append(node)
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in ("text", "content", "input", "output", "instructions", "arguments"):
                _strings(v, out, budget)
    elif isinstance(node, list):
        for v in node:
            _strings(v, out, budget)


def _response_items(db: Path) -> list[Item]:
    try:
        con = _ro(db)
        rows = con.execute(
            "SELECT response_id, data, accessed_at FROM responses ORDER BY accessed_at DESC LIMIT 500"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return _database_items(db, "response_store.db")
    items = []
    for rid, data, at in rows:
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data
        texts: list[str] = []
        _strings(parsed, texts)
        when = time.strftime("%Y-%m-%d", time.localtime(at or 0))
        items.append(
            _item(
                "response",
                "API transcripts · response_store.db",
                f"response_store.db/{rid}",
                "",
                f"{when} · {rid}",
                "\n\n".join(texts)[:4000] or data[:4000],
                publishable=False,
                note="transcripts cannot be published",
                path=str(db.resolve()),
            )
        )
    return items


def _cron_items(path: Path) -> list[Item]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _data_items(path, "cron/jobs.json", "Cron · cron")
    jobs = data.get("jobs", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    return [
        _item(
            "cron",
            "Cron · cron",
            f"cron/jobs.json#{j.get('id', i)}",
            "",
            str(j.get("name") or j.get("id") or f"job {i}"),
            json.dumps(j, ensure_ascii=False, indent=1)[:4000],
            publishable=False,
            note="cron jobs cannot be published",
            path=str(path.resolve()),
        )
        for i, j in enumerate(jobs)
        if isinstance(j, dict)
    ]


def _database_items(db: Path, rel: str, group: str = "") -> list[Item]:
    try:
        con = _ro(db)
        names = [
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        lines = []
        for n in names:
            rows = con.execute('SELECT count(*) FROM "' + n.replace('"', '""') + '"').fetchone()[0]
            lines.append(f"{n}: {rows} rows")
        con.close()
    except sqlite3.Error:
        lines = ["(unreadable)"]
    return [
        _item(
            "database",
            group or _group_for(rel),
            rel,
            "",
            f"{Path(rel).name} · database · {_size(db)}",
            "\n".join(lines),
            publishable=False,
            note="databases show their structure only",
            path=str(db.resolve()),
        )
    ]


def _data_items(path: Path, rel: str, group: str) -> list[Item]:
    text = _read(path) or ""
    return [
        _item(
            "data",
            group,
            rel,
            "",
            f"{Path(rel).name} · {_size(path)}",
            text[:4000] or "(empty)",
            publishable=False,
            note="structured data cannot be published",
            path=str(path.resolve()),
        )
    ]


def _file_item(path: Path, rel: str, group: str) -> list[Item]:
    stat = path.stat()
    return [
        _item(
            "file",
            group,
            rel,
            "",
            f"{Path(rel).name} · {_size(path)}",
            f"{display_path(path)}\n{_size(path)} · modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(stat.st_mtime))}",
            publishable=False,
            note="binary files show their path only",
            path=str(path.resolve()),
        )
    ]


def _group_for(rel: str, prefix: str = "Data dir") -> str:
    parts = rel.split("/")
    return f"{prefix} · {parts[0]}" if len(parts) > 1 else f"{prefix} · root"


def _generic_items(path: Path, rel: str, group: str, kind: str = "workspace") -> list[Item]:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES or path.name == ".cursorrules":
        text = _read(path)
        return _doc_items(kind, group, rel, text, path) if text else _file_item(path, rel, group)
    if suffix in DATA_SUFFIXES:
        return _data_items(path, rel, group)
    if _is_sqlite(path):
        return _database_items(path, rel, group)
    return _file_item(path, rel, group)


def _home_item(home: Path, path: Path, rel: str) -> list[Item]:
    if rel in ("memories/MEMORY.md", "memories/USER.md"):
        return []  # parsed entry by entry in _memory_items
    if rel == "SOUL.md":
        return _doc_items("soul", "Persona · SOUL.md", rel, _read(path) or "", path)
    if rel.startswith("skills/") and path.name == "SKILL.md":
        text = _read(path)
        return [_item("skill", "Skills", rel, "", path.parent.name, text, path=str(path.resolve()))] if text else []
    special = {
        "state.db": _session_items,
        "kanban.db": _kanban_items,
        "response_store.db": _response_items,
        "cron/jobs.json": _cron_items,
    }
    if rel in special:
        return special[rel](path) or [
            _item(
                "data",
                _group_for(rel),
                rel,
                "",
                f"{rel} · no records yet · {_size(path)}",
                "",
                publishable=False,
                note="expands once records exist",
                path=str(path.resolve()),
            )
        ]
    if rel.startswith("logs/"):
        items = _generic_items(path, rel, _group_for(rel))
        for it in items:
            it.publishable = False
            it.note = "logs can hold chat excerpts; the owner's file tools can read them"
        return items
    group = "Skills" if rel.startswith("skills/") else _group_for(rel)
    return _generic_items(path, rel, group)


def _config(home: Path) -> dict:
    try:
        from . import _yaml as yaml

        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _cwd(home: Path) -> Optional[Path]:
    cwd = (_config(home).get("terminal") or {}).get("cwd")
    return Path(cwd).expanduser().resolve() if isinstance(cwd, str) and cwd.strip() else None


def _runtime_items(home: Path) -> list[Item]:
    items = []
    provider = ((_config(home).get("memory") or {}).get("provider") or "").strip()
    if provider:
        items.append(
            _item(
                "provider",
                "External memory / plugins",
                f"memory.provider={provider}",
                "",
                f"External memory provider: {provider}",
                provider,
                publishable=False,
                note="the public side configures no memory provider (check: no external memory provider); its contents are not listed here",
            )
        )
    plugins = home / "plugins"
    if plugins.is_dir():
        for p in sorted(x for x in plugins.iterdir() if x.is_dir() and not x.name.startswith(".")):
            items.append(
                _item(
                    "plugin",
                    "External memory / plugins",
                    f"plugins/{p.name}",
                    "",
                    f"Plugin: {p.name}",
                    p.name,
                    publishable=False,
                    note="plugin context never enters a guest session",
                    path=str(p.resolve()),
                )
            )
    return items


def _bundled_skill_dirs(home: Path) -> set[Path]:
    """Official skills synced in by Hermes (tools/skills_sync.py manifest); they are not the owner's content."""
    try:
        names = {
            line.split(":", 1)[0].strip()
            for line in (home / "skills" / ".bundled_manifest").read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError:
        return set()
    return {p.parent.resolve() for p in (home / "skills").rglob("SKILL.md") if p.parent.name in names}


def _stamp(path: Path) -> tuple:
    out = []
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _memo(fresh: dict, key: tuple, path: Path, build) -> list[Item]:
    stamp = _stamp(path)
    hit = _MEMO.get(key)
    items = hit[1] if hit and hit[0] == stamp else build()
    fresh[key] = (stamp, items)
    return items


def scan(owner_home: Path, workspace_roots: list[Path]) -> list[Item]:
    """Everything under HERMES_HOME and the workspace roots, minus config/secrets/caches/add-ons (HOME_EXCLUDE).
    Every path is listed each time so new files show up at once; only changed files are re-read."""
    global _MEMO
    owner_home = Path(owner_home)
    home_abs = owner_home.resolve()
    roots = [Path(r).resolve() for r in workspace_roots if Path(r).is_dir()]
    seen: set[Path] = set()
    fresh: dict[tuple, tuple[tuple, list]] = {}
    items = _memory_items(owner_home) + _note_items(owner_home)
    bundled = _bundled_skill_dirs(owner_home)
    if bundled:
        items.append(
            _item(
                "skill",
                "Skills",
                "skills/.bundled_manifest",
                "",
                f"Built-in skills: {len(bundled)} (folded)",
                "\n".join(sorted(d.name for d in bundled)),
                publishable=False,
                note="shipped with Hermes, not your content",
                path=str((owner_home / "skills").resolve()),
            )
        )
    if owner_home.is_dir():
        skills = (owner_home / "skills").resolve()
        exclude = HOME_EXCLUDE | {
            f"skills/{d.parent.relative_to(skills).as_posix()}/DESCRIPTION.md" for d in bundled if d.parent != skills
        }
        # Workspace roots inside the home are listed as workspace, not as data dir.
        exclude |= {r.relative_to(home_abs).as_posix() for r in roots if home_abs in r.parents}
        for path, rel in _walk(owner_home, exclude, bundled, seen):
            items += _memo(fresh, ("home", str(home_abs), rel), path, lambda: _home_item(owner_home, path, rel))
    prompt_dirs = {r for r in roots} | ({_cwd(owner_home)} - {None})
    for root in roots:
        if root == home_abs:
            continue
        prefix = root.relative_to(home_abs).as_posix() if home_abs in root.parents else root.name
        for path, rel in _walk(root, set(), {home_abs}, seen):
            found = _memo(
                fresh,
                ("ws", str(root), rel),
                path,
                lambda: _generic_items(path, f"{prefix}/{rel}", _group_for(rel, f"Workspace · {root.name}")),
            )
            if path.name in CONTEXT_FILE_NAMES and path.parent.resolve() in prompt_dirs:
                for it in found:
                    it.note = "project context: Hermes puts this file in the system prompt when the session starts here"
            items += found
    items += _runtime_items(owner_home)
    _MEMO = fresh
    return items


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_allowlist(owner_home: Path) -> dict[str, dict]:
    try:
        data = json.loads((data_dir(owner_home) / ALLOWLIST_FILE).read_text(encoding="utf-8"))
        return {e["id"]: e for e in data.get("entries", []) if isinstance(e, dict) and e.get("id")}
    except (OSError, ValueError):
        return {}


APPROVAL_LOG = "approvals.jsonl"
VIAS = ("single", "bulk-A", "bulk-B", "bulk-C", "chat")


def approve(
    owner_home: Path,
    items: list[Item],
    public_ids: Iterable[str],
    public_texts: Optional[dict[str, str]] = None,
    decisions: Optional[dict[str, dict]] = None,
    classification: Optional[dict[str, dict]] = None,
    by: str = "owner",
) -> dict:
    """Save what the owner approved. ``public_texts`` maps an item id to the guest-facing rewrite; guests never
    get the original when a rewrite exists (an empty or identical rewrite means "publish the original").

    ``decisions`` says how each click happened (``single`` or ``bulk-A``/``bulk-B``/``bulk-C``). A bulk click
    only counts for items the classifier put in that tier (bulk-B only with a guest version), and never for a
    C item: those are refused and
    returned in ``rejected``. A single click may override the classifier (logged as an override). Every
    change is appended to the owner's approval log, so each published item traces back to one owner click."""
    from . import privacy

    if classification is None:
        from .classify import cached

        classification = cached(owner_home, items)
    wanted, texts, decisions = set(public_ids), public_texts or {}, decisions or {}
    previous = load_allowlist(owner_home)
    entries: dict[str, dict] = {}
    rejected, log = [], []
    now = int(time.time())
    for it in items:
        dec = decisions.get(it.id) if isinstance(decisions.get(it.id), dict) else {}
        via = dec.get("via") if dec.get("via") in VIAS else "single"
        cls = classification.get(it.id) or {}
        tier, rules_hit = cls.get("tier", ""), list(cls.get("rules_hit") or [])
        prev = previous.get(it.id)
        base = {"id": it.id, "title": it.title, "fingerprint": it.fingerprint, "decided_by": by, "decided_via": via,
                "classifier_tier": tier, "rules_hit": rules_hit}
        if it.id not in wanted or not it.publishable:
            if prev or dec:
                log.append({**base, "at": now, "decision": "private"})
            continue
        if via.startswith("bulk-") and (tier == "C" or via != f"bulk-{tier}"):
            rejected.append({"id": it.id, "title": it.title, "tier": tier, "via": via})
            if prev and prev.get("fingerprint") == it.fingerprint:
                entries[it.id] = prev
            continue
        rewrite = (texts.get(it.id) or "").strip()
        public_text = rewrite if rewrite and rewrite != it.text else ""
        if via == "bulk-B" and not public_text:
            rejected.append({"id": it.id, "title": it.title, "tier": tier, "via": via, "why": "no guest version"})
            continue
        if (prev and not dec and prev.get("fingerprint") == it.fingerprint
                and prev.get("public_text", "") == public_text):
            entries[it.id] = {**base, "decided_via": "single", **prev}  # unchanged since the owner approved it
            continue
        mode = "rewrite" if public_text else "as-is"
        entry = {**base, "kind": it.kind, "source": it.source, "approved_at": now, "mode": mode,
                 "override": tier == "C" or (tier == "B" and mode == "as-is")}
        if public_text:
            entry["public_text"] = public_text
        entries[it.id] = entry
        log.append({**{k: v for k, v in entry.items() if k != "public_text"}, "at": now, "decision": "public"})
    rules = privacy.load_rules(owner_home)
    shown = {i.id: privacy.screen({"text": entries[i.id].get("public_text") or i.text, "title": ""}, rules)
             for i in items if i.id in entries}
    combo = privacy.combination(shown, rules)
    for item_id in combo["ids"]:
        entries[item_id]["combination"] = True
    _atomic_write(
        data_dir(owner_home) / ALLOWLIST_FILE,
        json.dumps({"version": 2, "entries": list(entries.values())}, ensure_ascii=False, indent=2),
    )
    if log:
        path = data_dir(owner_home) / APPROVAL_LOG
        with path.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in log)
        os.chmod(path, 0o600)
    return {"entries": entries, "rejected": rejected, "combination": combo, "logged": len(log)}


def save_allowlist(
    owner_home: Path, items: list[Item], public_ids: Iterable[str], public_texts: Optional[dict[str, str]] = None
) -> dict[str, dict]:
    """``approve`` with single clicks only; returns the saved entries."""
    return approve(owner_home, items, public_ids, public_texts)["entries"]


def read_approval_log(owner_home: Path) -> list[dict]:
    try:
        lines = (data_dir(owner_home) / APPROVAL_LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def status(item: Item, allowlist: dict[str, dict]) -> str:
    """``public`` (approved, unchanged) | ``changed`` (approved, content edited since → treated as private) |
    ``private``."""
    entry = allowlist.get(item.id)
    if not entry or not item.publishable:
        return "private"
    return "public" if entry.get("fingerprint") == item.fingerprint else "changed"


def build_projection(items: list[Item], allowlist: dict[str, dict], owner_name: str = "the owner") -> dict:
    """Only items approved with an unchanged fingerprint; everything else stays out (private by default)."""
    public = [it for it in items if status(it, allowlist) == "public"]

    def shown(it: Item) -> str:
        return (allowlist.get(it.id, {}).get("public_text") or it.text).strip()

    def origin(it: Item) -> dict:
        entry = allowlist.get(it.id, {})
        return {"id": it.id, "tier": entry.get("classifier_tier", ""),
                "mode": "rewrite" if entry.get("public_text") else "as-is",
                "decided_via": entry.get("decided_via", "single")}

    def title(it: Item) -> str:
        # A rewrite replaces the title too: the original's file name and headings are the owner's, not the guest's.
        rewrite = allowlist.get(it.id, {}).get("public_text", "").strip()
        if not rewrite:
            return it.title
        lines = rewrite.splitlines()
        if lines[0].strip() == "---" and "---" in (l.strip() for l in lines[1:]):
            end = next(n for n, l in enumerate(lines[1:], 1) if l.strip() == "---")
            name = next((l.split(":", 1)[1].strip().strip("\"'") for l in lines[1:end] if l.startswith("name:")), "")
            lines = ([name] if name else []) + lines[end + 1:]
        first = next((l.lstrip("#").strip() for l in lines if l.strip()), "") or "Note"
        return first if len(first) <= 70 else first[:69] + "…"

    facts = [it for it in public if it.kind in ("memory", "user", "note")]
    methods = [it for it in public if it.kind == "skill"]
    return {
        "version": 2,
        "generated_at": int(time.time()),
        "owner_name": owner_name,
        "facts": [shown(it) for it in facts],
        "fact_meta": [{"text": shown(it), **origin(it)} for it in facts],
        "methods": [{"title": title(it), "text": shown(it), **origin(it)} for it in methods],
        "documents": [{"title": title(it), "text": shown(it), **origin(it)} for it in public
                      if it not in facts and it not in methods],
    }


def write_projection(
    owner_home: Path, public_home: Path, workspace_roots: list[Path], owner_name: str = "the owner"
) -> dict:
    """Projection only, no revocation handling. Use ``publish`` unless the caller handles revocation itself."""
    items = scan(owner_home, workspace_roots)
    projection = build_projection(items, load_allowlist(owner_home), owner_name)
    _atomic_write(data_dir(public_home) / PROJECTION_FILE, json.dumps(projection, ensure_ascii=False, indent=2), 0o644)
    return projection


def read_projection(public_home: Path) -> dict:
    try:
        data = json.loads((data_dir(public_home) / PROJECTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"facts": [], "documents": []}
    return data if isinstance(data, dict) else {"facts": [], "documents": []}


def publish(
    owner_home: Path,
    public_home: Path,
    workspace_roots: list[Path],
    owner_name: str = "the owner",
    hermes_bin: Optional[str] = None,
) -> dict:
    """The one publishing path (dashboard, demo board and ``hermes salts-secrets sync``): rewrite the projection,
    then, if anything guests could see before is gone, archive the guest sessions for the owner and delete
    them from the public store, so a revoked fact does not live on in a guest transcript or summary."""
    from . import guests

    before = read_projection(public_home)
    after = write_projection(owner_home, public_home, workspace_roots, owner_name)
    return {"projection": after, **guests.revoke_and_purge(owner_home, public_home, before, after, hermes_bin)}


def sync_public(
    owner_home: Path, public_home: Path, workspace_roots: list[Path], owner_name: str = "the owner"
) -> dict:
    """``publish`` returning only the projection (revoked items still purge guest sessions)."""
    return publish(owner_home, public_home, workspace_roots, owner_name)["projection"]
