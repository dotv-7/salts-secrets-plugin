"""Owner profile: tell guests something from chat, a voice message included.

The owner agent may only queue a note (``salt_propose_public``); it never publishes. A note crosses when the
owner sends ``/salt approve <id>`` from their own DM, a command the gateway hands to this plugin directly, so
no model output can approve one. Approved notes are ordinary Context items: logged with ``decided_via=chat``,
revocable on the Context page like everything else.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from . import context, privacy
from .home import current, data_dir
from .readonly import VIEWER_PREFIX

PENDING_FILE = "pending_notes.json"
MAX_CHARS = 1000
TOOL_NAME = "salt_propose_public"
SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Queue a note for strangers (the public profile) when the owner asks you to tell guests, visitors or "
        "the public something. Write the guest-safe version yourself: no real names, addresses, contacts, "
        "accounts, health, family or exact dates. This does NOT publish. Show the owner the text and the id, "
        "and tell them to send `/salt approve <id>` (or `/salt drop <id>`)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The note exactly as guests should read it."}},
        "required": ["text"],
    },
}


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [n for n in data if isinstance(n, dict)] if isinstance(data, list) else []


def _write(path: Path, rows: list[dict]) -> None:
    context._atomic_write(path, json.dumps(rows, ensure_ascii=False, indent=2))


def pending(home: Path) -> list[dict]:
    return _read(data_dir(home) / PENDING_FILE)


def propose(home: Path, text: str) -> dict:
    """Queue one note; a note the privacy rules put in C is refused, so it never reaches the queue."""
    text = str(text or "").strip()
    if not text or len(text) > MAX_CHARS:
        return {"status": "refused", "why": f"a note is 1-{MAX_CHARS} characters"}
    hit = privacy.screen({"text": text, "title": ""}, privacy.load_rules(home))
    if hit["floor"] == "C":
        return {"status": "refused", "rules_hit": hit["rules_hit"],
                "why": "these rules say this must never be public; rewrite it without those details"}
    note = {"id": secrets.token_hex(3), "text": text, "rules_hit": hit["rules_hit"], "floor": hit["floor"],
            "proposed_at": int(time.time())}
    _write(data_dir(home) / PENDING_FILE, pending(home) + [note])
    return {"status": "pending", **note, "next": f"/salt approve {note['id']}"}


def drop(home: Path, note_id: str) -> bool:
    rows = pending(home)
    keep = [n for n in rows if n.get("id") != note_id]
    _write(data_dir(home) / PENDING_FILE, keep)
    return len(keep) != len(rows)


def _settings(home: Path) -> dict:
    from . import cli

    return cli._settings(cli._read_yaml(home / "config.yaml"))


def approve(home: Path, note_id: str) -> dict:
    """Move one pending note into the owner's notes, approve it as-is, and publish."""
    from .classify import cached

    note = next((n for n in pending(home) if n.get("id") == note_id), None)
    if note is None:
        return {"ok": False, "why": f"no pending note {note_id}"}
    notes_path = data_dir(home) / context.NOTES_FILE
    _write(notes_path, _read(notes_path) + [{"id": note["id"], "text": note["text"], "added_at": int(time.time())}])
    drop(home, note_id)
    cfg = _settings(home)
    roots = [Path(p).expanduser() for p in cfg.get("workspaces") or [home / "workspace"]]
    items = context.scan(home, roots)
    item = next(i for i in items if i.kind == "note" and i.source.endswith(context.NOTES_FILE)
                and i.title.startswith(f"Note {note_id}:"))
    allow = context.load_allowlist(home)
    cls = cached(home, items)
    cls[item.id] = {"tier": note.get("floor", ""), "rules_hit": note.get("rules_hit", [])}
    context.approve(home, items, set(allow) | {item.id},
                    {k: v.get("public_text", "") for k, v in allow.items()},
                    {item.id: {"via": "chat"}}, cls)
    public = home / "profiles" / (cfg.get("public_profile") or "public")
    result = context.publish(home, public, roots, cfg.get("owner_name") or "the owner")
    return {"ok": True, "facts": len(result["projection"]["facts"])}


HELP = ("/salt: list notes waiting for you · /salt approve <id>: publish one to guests · "
        "/salt drop <id>: discard it. Ask me to tell guests something (typing or a voice message) to queue one.")


def make_command(owners: set[str], home_fn: Callable[[], Path] = current):
    from .route import is_owner_dm

    def command(raw_args: str = "", source: Any = None, session_id: str = "", **_: Any) -> str:
        if str(session_id or "").startswith(VIEWER_PREFIX) or not is_owner_dm(SimpleNamespace(source=source), owners):
            return "Only the owner, in their own DM, can approve notes for guests."
        home = home_fn()
        sub, _, arg = str(raw_args or "").strip().partition(" ")
        arg = arg.strip()
        if sub in ("", "pending", "list"):
            rows = pending(home)
            if not rows:
                return "No notes waiting. " + HELP
            return "\n".join(f"{n['id']} · {n.get('floor') or 'A'} · {n['text'][:120]}" for n in rows)
        if sub == "approve" and arg:
            out = approve(home, arg)
            return f"Published note {arg}. Guests now have {out['facts']} facts." if out["ok"] else out["why"]
        if sub == "drop" and arg:
            return f"Dropped note {arg}." if drop(home, arg) else f"No pending note {arg}."
        return HELP

    return command


def _handle(args: Optional[dict] = None, **_: Any) -> str:
    return json.dumps(propose(current(), (args or {}).get("text", "")), ensure_ascii=False)


def register(ctx) -> None:
    owners = {str(o).strip().lower() for o in ctx.get_config("owners", default=[]) or [] if str(o).strip()}
    ctx.register_tool(name=TOOL_NAME, toolset="salts_secrets", schema=SCHEMA, handler=_handle,
                      description=SCHEMA["description"], emoji="📝")
    ctx.register_command("salt", make_command(owners), description="Notes for guests: list, approve, drop",
                         args_hint="[approve|drop <id>]")
