"""Permission tiers: which toolsets each non-owner tier may use.

The owner's ``permissions.json`` is the source of truth. ``sync`` writes each tier profile's ``tier.json``
(read by the tool gate) and narrows its ``platform_toolsets`` so the model only sees allowed tools. Tier
profiles share the owner's machine, so anything that can read the filesystem or act as the owner stays
locked whatever the policy file says.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from pathlib import Path
from typing import Iterable, Optional

from . import _yaml as yaml
from .home import NAME, PUBLIC_KEY_ENV, data_dir

POLICY_FILE = "permissions.json"
TIER_FILE = "tier.json"
ISOLATED_CWD = "cwd"
BLOCK_TTL = 90 * 86400
SEEN_MAX = 5
# Files Hermes auto-loads into the system prompt from the working directory (agent/prompt_builder.py).
CONTEXT_FILE_NAMES = (
    ".hermes.md",
    "HERMES.md",
    "AGENTS.md",
    "AGENTS.override.md",
    "agents.md",
    "CLAUDE.md",
    ".cursorrules",
    ".cursor",
)
TIERS = ("owner", "admin", "user", "guest", "blocked")
CONFIGURABLE = ("admin", "user", "guest")
TIER_LABEL = {"owner": "Owner", "admin": "Admin", "user": "User", "guest": "Guest", "blocked": "Blocked"}

# toolset -> (tools, category, label, lock reason for non-owner tiers or "", shared-state warning or "")
CATALOG: dict[str, tuple[tuple[str, ...], str, str, str, str]] = {
    "web": (("web_search", "web_extract"), "Web", "Search and fetch pages", "", ""),
    "x_search": (("x_search",), "Web", "X/Twitter search", "", ""),
    "vision": (("vision_analyze",), "Images and video", "Look at images (URLs only)", "", ""),
    "video": (("video_analyze",), "Images and video", "Watch video", "", ""),
    "image_gen": (("image_generate",), "Generate", "Generate images", "", "spends your API budget"),
    "video_gen": (
        ("video_generate", "xai_video_edit", "xai_video_extend"),
        "Generate",
        "Generate video",
        "",
        "spends your API budget",
    ),
    "tts": (("text_to_speech",), "Generate", "Read aloud", "", ""),
    "clarify": (("clarify",), "Conversation", "Ask a clarifying question", "", ""),
    "public_notes": (("public_notes_lookup",), "Conversation", "Look up public notes", "", ""),
    "todo": (("todo_list",), "Conversation", "To-do list", "", ""),
    "memory": (("memory",), "Memory", "Memory (the tier's own)", "", "shared by everyone in the tier"),
    "session_search": (
        ("session_search",),
        "Memory",
        "Search past sessions (the tier's own)",
        "",
        "members of the tier can search each other's sessions",
    ),
    "skills": (
        ("skills_list", "skill_view", "skill_manage"),
        "Skills",
        "Skills (the tier's own)",
        "",
        "shared by everyone in the tier",
    ),
    "terminal": (
        ("terminal", "process_manage"),
        "Terminal and code",
        "Terminal",
        "can read any file on your machine",
        "",
    ),
    "code_execution": (("execute_code",), "Terminal and code", "Run code", "can read any file on your machine", ""),
    "file": (
        ("read_file", "write_file", "patch", "search_files"),
        "Files",
        "Read and write files",
        "can read your private files",
        "",
    ),
    "browser": (
        ("browser_exec", "browser_cdp", "browser_dialog", "browser_vault"),
        "Browser",
        "Browser (4 tools, v0.23.0)",
        "can use sites you are signed in to and open local files",
        "",
    ),
    "computer_use": (("computer_use",), "Browser", "Computer use", "can drive your desktop", ""),
    "cronjob": (("cronjob_manage",), "Scheduling", "Scheduled jobs", "runs as you, indefinitely", ""),
    "delegation": (("delegate_task",), "Scheduling", "Delegate sub-tasks", "sub-tasks bypass the gate", ""),
    "kanban": (("kanban_create",), "Scheduling", "Kanban tasks", "can see your tasks", ""),
    "connections": (("manage_connections",), "Outbound", "Account connections", "can use your accounts", ""),
    "messaging": (("send_message",), "Outbound", "Send messages", "sends messages in your name", ""),
}
SAFE_TOOLSETS = frozenset(k for k, v in CATALOG.items() if not v[3])
# A tier's own memory, sessions, skills and to-dos are for the planned Admin/User tiers; guests never get them.
TIER_OWN = frozenset({"todo", "memory", "session_search", "skills"})
GUEST_TOOLSETS = SAFE_TOOLSETS - TIER_OWN
DEFAULT_TIERS = {
    "admin": [
        "web",
        "x_search",
        "vision",
        "video",
        "image_gen",
        "tts",
        "clarify",
        "public_notes",
        "todo",
        "memory",
        "session_search",
        "skills",
    ],
    "user": ["web", "vision", "image_gen", "tts", "clarify", "public_notes", "todo", "memory"],
    "guest": ["web", "vision", "image_gen", "tts", "clarify", "public_notes"],
}


def tools_for(toolsets: Iterable[str]) -> list[str]:
    return sorted({t for ts in toolsets if ts in SAFE_TOOLSETS for t in CATALOG[ts][0]})


def _clean(toolsets: Iterable[str], tier: str = "") -> list[str]:
    allowed = GUEST_TOOLSETS if tier == "guest" else SAFE_TOOLSETS
    return [k for k in CATALOG if k in set(toolsets) and k in allowed]


def _live_blocks(entries, now: float) -> list[dict]:
    return [
        b
        for b in entries or []
        if isinstance(b, dict)
        and b.get("kind") in ("ip", "device")
        and isinstance(b.get("value"), str)
        and b["value"]
        and b.get("until", 0) > now
    ]


def load(owner_home: Path) -> dict:
    try:
        data = json.loads((data_dir(owner_home) / POLICY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    tiers = data.get("tiers") if isinstance(data.get("tiers"), dict) else {}
    return {
        "tiers": {t: _clean(tiers.get(t, DEFAULT_TIERS[t]), t) for t in CONFIGURABLE},
        "members": [m for m in data.get("members") or [] if isinstance(m, dict) and m.get("tier") in TIERS[1:]],
        "blocklist": _live_blocks(data.get("blocklist"), time.time()),
        "guest_access": data.get("guest_access", True) is not False,
    }


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def save(owner_home: Path, policy: dict) -> dict:
    clean = {
        "tiers": {t: _clean((policy.get("tiers") or {}).get(t, []), t) for t in CONFIGURABLE},
        "members": policy.get("members") or [],
        "blocklist": _live_blocks(policy.get("blocklist"), time.time()),
        "guest_access": policy.get("guest_access", True) is not False,
    }
    _write(data_dir(owner_home) / POLICY_FILE, clean)
    return clean


# Members and invite codes


def _hash(code: str, salt: bytes) -> str:
    return hashlib.scrypt(code.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def add_member(owner_home: Path, name: str, tier: str) -> tuple[dict, str]:
    if tier not in TIERS[1:]:
        raise ValueError("bad tier")
    policy = load(owner_home)
    code = "inv-" + secrets.token_urlsafe(12)
    salt = secrets.token_bytes(16)
    member = {
        "id": secrets.token_hex(6),
        "name": name.strip()[:40] or "unnamed",
        "tier": tier,
        "salt": salt.hex(),
        "code_hash": _hash(code, salt),
        "created": int(time.time()),
    }
    policy["members"].append(member)
    save(owner_home, policy)
    return member, code


def update_member(owner_home: Path, member_id: str, tier: Optional[str] = None, delete: bool = False) -> bool:
    policy = load(owner_home)
    found = next((m for m in policy["members"] if m.get("id") == member_id), None)
    if not found or (tier is not None and tier not in TIERS[1:]):
        return False
    if delete:
        policy["members"].remove(found)
    elif tier:
        found["tier"] = tier
        if tier == "blocked":
            _block_member(policy, found)
        else:
            policy["blocklist"] = [b for b in policy["blocklist"] if b.get("member") != member_id]
    save(owner_home, policy)
    return True


# Membership lives in the browser cookie, so a blocked member could otherwise clear it and come back as a
# Guest. Every device id and IP a member was seen from is remembered; blocking turns them into blocklist
# entries that the chat server checks before anything else. IP entries expire (shared NAT / mobile IPs).
def _add_block(policy: dict, kind: str, value: str, member: dict) -> None:
    if not value or any(b["kind"] == kind and b["value"] == value for b in policy["blocklist"]):
        return
    policy["blocklist"].append(
        {
            "kind": kind,
            "value": value,
            "member": member.get("id", ""),
            "name": member.get("name", ""),
            "since": int(time.time()),
            "until": int(time.time() + BLOCK_TTL),
        }
    )


def _block_member(policy: dict, member: dict) -> None:
    for ip in member.get("seen_ips") or []:
        _add_block(policy, "ip", ip, member)
    for did in member.get("seen_devices") or []:
        _add_block(policy, "device", did, member)


_SHARED_NETS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
)


def blockable_ip(ip: str) -> bool:
    """Only public client IPs are blocked: loopback/LAN/container addresses usually mean a reverse proxy that
    is not trusted for X-Forwarded-For, and blocking it would lock out every visitor."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_loopback or a.is_link_local or a.is_unspecified or any(a in n for n in _SHARED_NETS))


def note_seen(owner_home: Path, member_id: str, ip: str, device: str) -> None:
    """Remember where a member connects from (last few IPs/devices); a blocked member's new ones get blocked too."""
    policy = load(owner_home)
    m = next((x for x in policy["members"] if x.get("id") == member_id), None)
    if m is None:
        return
    changed = False
    for field, value in (("seen_ips", ip if blockable_ip(ip) else ""), ("seen_devices", device)):
        seen = [v for v in m.get(field) or [] if isinstance(v, str)]
        if value and value not in seen:
            m[field] = (seen + [value])[-SEEN_MAX:]
            changed = True
    if m.get("tier") == "blocked":
        before = len(policy["blocklist"])
        _block_member(policy, m)
        changed = changed or len(policy["blocklist"]) != before
    if changed:
        save(owner_home, policy)


def is_blocked(owner_home: Path, ip: str, device: str) -> Optional[dict]:
    return next(
        (
            b
            for b in load(owner_home)["blocklist"]
            if (b["kind"] == "ip" and ip and b["value"] == ip)
            or (b["kind"] == "device" and device and b["value"] == device)
        ),
        None,
    )


def unblock(owner_home: Path, kind: str, value: str) -> bool:
    policy = load(owner_home)
    keep = [b for b in policy["blocklist"] if not (b["kind"] == kind and b["value"] == value)]
    if len(keep) == len(policy["blocklist"]):
        return False
    policy["blocklist"] = keep
    save(owner_home, policy)
    return True


def set_guest_access(owner_home: Path, enabled: bool) -> None:
    policy = load(owner_home)
    policy["guest_access"] = bool(enabled)
    save(owner_home, policy)


def find_member(owner_home: Path, code: str) -> Optional[dict]:
    for m in load(owner_home)["members"]:
        try:
            if hmac.compare_digest(_hash(code, bytes.fromhex(m["salt"])), m["code_hash"]):
                return m
        except (KeyError, ValueError, TypeError):
            continue
    return None


def member_by_id(owner_home: Path, member_id: str) -> Optional[dict]:
    return next((m for m in load(owner_home)["members"] if m.get("id") == member_id), None)


def public_member(m: dict) -> dict:
    return {k: m.get(k) for k in ("id", "name", "tier", "created")}


def sync(owner_home: Path, tier_homes: dict[str, Path]) -> dict[str, list[str]]:
    """Write tier.json + narrow platform_toolsets for every tier profile that exists."""
    policy = load(owner_home)
    done = {}
    for tier, home in tier_homes.items():
        home = Path(home)
        if tier not in CONFIGURABLE or not home.is_dir():
            continue
        toolsets = policy["tiers"][tier]
        _write(data_dir(home) / TIER_FILE, {"tier": tier, "toolsets": toolsets, "allowed_tools": tools_for(toolsets)})
        _narrow_config(home / "config.yaml", toolsets)
        done[tier] = toolsets
    return done


def isolated_cwd(home: Path) -> Path:
    """Empty working directory for a tier profile, so Hermes finds no project context files
    (AGENTS.md, .hermes.md, CLAUDE.md, ...) to inject. Without an explicit ``terminal.cwd`` Hermes falls
    back to MESSAGING_CWD or the home directory, which can be the owner's workspace."""
    path = data_dir(home) / ISOLATED_CWD
    path.mkdir(parents=True, exist_ok=True)
    return path


def platform_keys() -> list[str]:
    """Every platform key a turn can resolve toolsets by; an unset key falls back to the full
    ``hermes-<platform>`` toolset, so a tier profile pins each one (natively routed guests use them)."""
    from gateway.config import Platform

    return sorted({p.value for p in Platform} | {"cli", "cron"})


def _narrow_config(path: Path, toolsets: list[str]) -> None:
    if not path.exists():
        return
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    pinned = cfg.setdefault("platform_toolsets", {})
    for key in platform_keys():
        pinned[key] = list(toolsets)
    mem = cfg.setdefault("memory", {})
    mem["memory_enabled"] = mem["user_profile_enabled"] = "memory" in toolsets
    mem["provider"] = ""
    cfg.setdefault("tools", {}).setdefault("tool_search", {})["enabled"] = "off"
    cfg.setdefault("terminal", {})["cwd"] = str(isolated_cwd(path.parent).resolve())
    cfg.setdefault("security", {})["allow_private_urls"] = False
    plugins = cfg.setdefault("plugins", {})
    enabled = [p for p in plugins.get("enabled") or [] if isinstance(p, str)]
    plugins["enabled"] = enabled if NAME in enabled else enabled + [NAME]
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _context_files_near(cwd: Path) -> list[str]:
    """Context files Hermes would load for this cwd: in cwd itself, plus the git-root chain if cwd is in a repo."""
    cwd = cwd.resolve()
    root = next((p for p in (cwd, *cwd.parents) if (p / ".git").exists()), None)
    dirs = [cwd] if root is None else [cwd, *[p for p in cwd.parents if p.is_relative_to(root)]]
    found = [str(d / n) for d in dirs for n in CONTEXT_FILE_NAMES if (d / n).exists()]
    if root is not None:
        found.append(f"{root} is a git repository (AGENTS.md chain walks up to it)")
    return found


def _profile_config(home: Path) -> dict:
    try:
        return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def plugin_ready(home: Path) -> bool:
    enabled = ((_profile_config(home).get("plugins") or {}).get("enabled")) or []
    return NAME in enabled and (Path(home) / "plugins" / NAME / "__init__.py").exists()


def profile_secrets(home: Path) -> dict[str, str]:
    from agent.secret_scope import build_profile_secret_scope

    try:
        return build_profile_secret_scope(Path(home))
    except Exception:  # noqa: BLE001
        return {}


def _truthy(value) -> Optional[bool]:
    if value is None or value == "":
        return None
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def preflight(owner_home: Path, after_setup: bool = True) -> list[dict]:
    """The three gateway settings the isolation needs, each with the command that fixes it.

    ``after_setup=False`` (start of ``setup``) only fails on values the owner set explicitly the wrong way:
    ``setup`` itself switches on multiplexing and writes an API key when they are merely missing."""
    cfg = _profile_config(owner_home)
    owner_secrets = profile_secrets(owner_home)
    out = []
    isolation = str((cfg.get("plugins") or {}).get("isolation") or "in_process").strip().lower().replace("-", "_")
    out.append({
        "ok": isolation == "in_process",
        "text": f"owner: plugins.isolation is in_process (now {isolation}); under the plugin host the routing "
        "hook cannot reply and the CLI command is not wired",
        "fix": "hermes config set plugins.isolation in_process",
    })
    api_cfg = ((cfg.get("platforms") or {}).get("api_server") or {})
    disabled = _truthy(api_cfg.get("enabled")) is False or _truthy(owner_secrets.get("API_SERVER_ENABLED")) is False
    key = owner_secrets.get("API_SERVER_KEY", "")
    out.append({
        "ok": not disabled and (len(key.strip()) >= 16 or not after_setup),
        "text": "owner: API server is enabled with a key of 16+ characters (guests outside the routes are answered through /p/<profile>/v1)",
        "fix": "hermes config set API_SERVER_ENABLED true  # and remove platforms.api_server.enabled: false"
        if disabled
        else "hermes config set API_SERVER_KEY <random 32 characters>",
    })
    multiplex = (cfg.get("gateway") or {}).get("multiplex_profiles")
    out.append({
        "ok": bool(multiplex) if after_setup else multiplex is not False,
        "text": "owner: gateway.multiplex_profiles is on (the /p/<profile> entrance exists)",
        "fix": "hermes config set gateway.multiplex_profiles true",
    })
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def routing_loaded(owner_home: Path) -> tuple[bool, str]:
    """Proof that the running gateway registered the routing hook: a heartbeat written by ``route.register``
    in the process ``gateway_state.json`` names (same pid, same start time). A config file can say the
    plugin is enabled while the plugin failed to load; then the owner agent would answer strangers."""
    from .route import HEARTBEAT_FILE

    try:
        state = json.loads((Path(owner_home) / "gateway_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "no gateway_state.json: the gateway has not run (hermes gateway restart)"
    pid = state.get("pid")
    if not isinstance(pid, int) or state.get("gateway_state") in ("stopped", "startup_failed"):
        return False, "the gateway is not running (hermes gateway restart)"
    # A dashboard in another container sees a different pid namespace; there the matching
    # start_time below is the proof, since a dead gateway leaves gateway_state "running" only after a crash.
    visible = _pid_alive(pid)
    if not visible and state.get("gateway_state") != "running":
        return False, "the gateway is not running (hermes gateway restart)"
    try:
        beats = json.loads((data_dir(owner_home) / HEARTBEAT_FILE).read_text(encoding="utf-8")).get("processes") or {}
    except (OSError, ValueError, AttributeError):
        beats = {}
    beat = beats.get(str(pid)) or {}
    if not beat:
        return False, f"gateway pid {pid} never registered the routing hook (plugin failed to load? see gateway logs)"
    if state.get("start_time") is not None and beat.get("start_time") != state.get("start_time"):
        return False, f"routing heartbeat is from an older process with pid {pid}; restart the gateway"
    return True, f"gateway pid {pid}" + ("" if visible else ", seen from another container")


def isolation_check(owner_home: Path, public_home: Path, gateway: bool = True) -> dict:
    """Every condition the isolation relies on, checked against the profiles as they are right now.
    ``gateway=False`` skips the running-gateway proof (used by ``setup``, before the restart)."""
    owner_home, public_home = Path(owner_home), Path(public_home)
    checks: list[dict] = []

    def add(ok: bool, text: str, fix: str = "") -> None:
        checks.append({"ok": bool(ok), "text": text, **({"fix": fix} if fix and not ok else {})})

    owner_cfg, public_cfg = _profile_config(owner_home), _profile_config(public_home)
    add(plugin_ready(owner_home), f"owner: {NAME} installed and enabled (strangers never reach the owner agent)")
    for item in preflight(owner_home):
        add(item["ok"], item["text"], item["fix"])
    owners = (((owner_cfg.get("plugins") or {}).get("entries") or {}).get(NAME) or {}).get("settings", {}).get("owners")
    add(
        bool(owners),
        "owner: your identity is set explicitly (settings.owners); without it even your own DM gets the public profile",
        "hermes salts-secrets setup --owner telegram:<your id>",
    )
    from . import routes

    route_problems = routes.problems(
        owner_cfg, owners, routes.profile_name(owner_home), public_home.name, profile_secrets(owner_home)
    )
    add(
        not route_problems,
        "owner: profile_routes send only the owner to the owner profile and everyone else to the public one"
        + ("" if not route_problems else " — " + "; ".join(route_problems)),
        "hermes salts-secrets setup   (rewrites its routes)",
    )
    if gateway:
        loaded, detail = routing_loaded(owner_home)
        add(loaded, "owner: the running gateway loaded the routing hook" + (f" ({detail})" if loaded else f" — {detail}"))
    add(public_home.is_dir(), f"public profile exists: profiles/{public_home.name}")
    problems = audit(public_home, GUEST_TOOLSETS)
    add(
        not problems,
        "public: tool audit passes (no terminal, files, sessions or context files)"
        + ("" if not problems else " — " + "; ".join(problems)),
    )
    memory = public_cfg.get("memory") or {}
    add(
        memory.get("memory_enabled") is False and memory.get("user_profile_enabled") is False,
        "public: MEMORY.md and USER.md injection switched off",
    )
    add((data_dir(public_home) / "public_context.json").exists(), "public: approved projection has been published")
    from .guests import unpurged

    left = unpurged(public_home)
    add(
        not left,
        "public: every guest session a revoke had to delete is gone"
        + ("" if not left else f" — {len(left)} still stored (their guests were moved to fresh sessions)"),
        "hermes salts-secrets sync   (retries the purge)",
    )
    owner_secrets, public_secrets = profile_secrets(owner_home), profile_secrets(public_home)
    public_key = owner_secrets.get(PUBLIC_KEY_ENV, "")
    add(
        bool(public_key) and public_key == public_secrets.get("API_SERVER_KEY"),
        f"keys: owner's {PUBLIC_KEY_ENV} matches the public profile's API_SERVER_KEY",
    )
    add(
        not public_key or public_key != owner_secrets.get("API_SERVER_KEY"),
        "keys: public key differs from the owner key",
    )
    ok = all(c["ok"] for c in checks)
    failing = [c["text"].split(" — ")[0].split(" (")[0] for c in checks if not c["ok"]]
    line = (
        f"ISOLATION OK · {len(checks)} checks"
        if ok
        else f"ISOLATION BROKEN · {len(failing)} of {len(checks)} failing: " + "; ".join(failing)
    )
    return {"ok": ok, "line": line, "checks": checks, "checked_at": int(time.time())}


def audit(home: Path, allowed: frozenset = SAFE_TOOLSETS) -> list[str]:
    """Isolation problems of a non-owner tier profile; an empty list means the hard isolation is in place."""
    home = Path(home)
    if not home.is_dir():
        return ["profile does not exist"]
    path = home / "config.yaml"
    if not path.exists():
        return ["config.yaml is missing"]
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return [f"config.yaml cannot be parsed: {exc}"]
    problems = []
    pinned = cfg.get("platform_toolsets") or {}
    unset = [key for key in platform_keys() if not isinstance(pinned.get(key), list)]
    if unset:
        problems.append("platform_toolsets not pinned (full default toolset): " + ", ".join(unset))
    toolsets = {t for key in platform_keys() if isinstance(pinned.get(key), list) for t in pinned[key]}
    bad = sorted(str(t) for t in toolsets if t not in allowed)
    if bad:
        problems.append("toolsets that are owner-only are enabled: " + ", ".join(map(str, bad)))
    if str(((cfg.get("tools") or {}).get("tool_search") or {}).get("enabled", "")).lower() not in ("off", "false"):
        problems.append("tools.tool_search is on (its tool_call bridge bypasses the tool list)")
    if (cfg.get("memory") or {}).get("provider"):
        problems.append("an external memory provider is configured")
    if (cfg.get("security") or {}).get("allow_private_urls") is not False:
        problems.append("security.allow_private_urls is not false (web tools could reach the LAN)")
    if not plugin_ready(home):
        problems.append(f"{NAME} is not installed or not enabled (no tool gate)")
    cwd = (cfg.get("terminal") or {}).get("cwd")
    if not cwd or str(cwd) in (".", "auto", "cwd"):
        problems.append(
            "terminal.cwd is not set: Hermes falls back to MESSAGING_CWD or the home directory, whose AGENTS.md etc. would enter the guest prompt"
        )
    elif not Path(cwd).is_dir():
        problems.append(f"terminal.cwd does not exist: {cwd}")
    else:
        problems += [
            f"context file in the working directory would be injected: {f}" for f in _context_files_near(Path(cwd))
        ]
    return problems
