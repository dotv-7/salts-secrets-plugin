"""The ``gateway.profile_routes`` entries ``setup`` writes into the owner profile, and their audit.

Hermes routes by sender since #115642 (fixes #33548): a route naming the owner's ``user_id`` sends the
owner to the owner profile, a platform-wide catch-all sends everyone else to the public profile, which
then answers through the native reply pipeline (streaming, media). A route has no DM/group field, so a
bare ``user_id`` route would also pull the owner's group messages into the owner agent:

- Telegram: a DM's chat id is the user id, so the owner route pins ``chat_id`` too and the owner's
  group messages fall through to the catch-all;
- Discord: a DM channel id is unknown at setup time, so the owner route is ``user_id`` only and the
  owner hook (``route.make_hook``) answers the owner's server messages through the public profile.

Other platforms get no routes; there the owner hook keeps answering guests over the local API.

The Discord adapter also checks ``DISCORD_ALLOWED_USERS`` itself, before any route, and ignores
``DISCORD_ALLOW_ALL_USERS`` while that list names anyone: a stranger never reaches the catch-all unless
the list holds ``*`` (``open_allowlist``).
"""

from __future__ import annotations

from pathlib import Path

from .home import NAME

PREFIX = f"{NAME}:"
ALLOW_ALL_ENV = {"telegram": "TELEGRAM_ALLOW_ALL_USERS", "discord": "DISCORD_ALLOW_ALL_USERS"}
DM_CHAT_IS_USER = {"telegram"}
ADAPTER_ALLOWLIST_ENV = {"discord": "DISCORD_ALLOWED_USERS"}


def open_allowlist(raw: str) -> str:
    """The allowlist with ``*`` added, or "" when it already admits everyone (empty, or holding ``*``)."""
    entries = [e.strip() for e in str(raw or "").split(",") if e.strip()]
    return "" if not entries or "*" in entries else ",".join(entries + ["*"])


def profile_name(home: Path) -> str:
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"


def owner_ids(owners) -> dict[str, list[str]]:
    by_platform: dict[str, list[str]] = {}
    for owner in owners or []:
        platform, _, uid = str(owner).partition(":")
        if platform in ALLOW_ALL_ENV and uid:
            by_platform.setdefault(platform, []).append(uid)
    return by_platform


def guest_routes(owners, owner_profile: str, public: str) -> list[dict]:
    routes = []
    for platform, uids in sorted(owner_ids(owners).items()):
        for uid in uids:
            route = {"name": f"{PREFIX}owner-{platform}-{uid}", "platform": platform, "user_id": uid}
            if platform in DM_CHAT_IS_USER:
                route["chat_id"] = uid
            routes.append({**route, "profile": owner_profile})
        routes.append({"name": f"{PREFIX}guests-{platform}", "platform": platform, "profile": public})
    return routes


def apply(cfg: dict, owners, owner_profile: str, public: str) -> list[dict]:
    """Replace the routes this plugin wrote before; the owner's own routes stay, ahead of ours."""
    gateway = cfg.setdefault("gateway", {})
    keep = [r for r in gateway.get("profile_routes") or [] if not str((r or {}).get("name", "")).startswith(PREFIX)]
    ours = guest_routes(owners, owner_profile, public)
    gateway["profile_routes"] = keep + ours
    return ours


def _enabled(routes) -> list[dict]:
    return [r for r in routes or [] if isinstance(r, dict) and r.get("enabled", True) is not False]


def problems(cfg: dict, owners, owner_profile: str, public: str, secrets: dict) -> list[str]:
    """Why a stranger could reach the owner agent through the routes, or a guest could not reach the public one."""
    routes = _enabled((cfg.get("gateway") or {}).get("profile_routes"))
    ids = owner_ids(owners)
    out = []
    for platform in sorted(ALLOW_ALL_ENV):
        mine = [r for r in routes if str(r.get("platform", "")).lower() == platform]
        catch_all = any(
            r.get("profile") == public and not any(r.get(k) for k in ("user_id", "chat_id", "guild_id", "thread_id"))
            for r in mine
        )
        if platform in ids and not catch_all:
            out.append(f"{platform}: no catch-all route to {public}")
        listed = ADAPTER_ALLOWLIST_ENV.get(platform)
        if catch_all and listed and open_allowlist(secrets.get(listed, "")):
            out.append(f"{platform}: {listed} names users but not '*', so the adapter drops every stranger "
                       f"before the catch-all route to {public}")
        allow_all = str(secrets.get(ALLOW_ALL_ENV[platform], "")).strip().lower() in ("1", "true", "yes", "on")
        if allow_all and not catch_all:
            out.append(f"{platform}: {ALLOW_ALL_ENV[platform]} is on without a catch-all route to {public}")
        for r in mine:
            if r.get("profile") != owner_profile:
                continue
            uid, chat = str(r.get("user_id") or ""), str(r.get("chat_id") or "")
            name = r.get("name") or "unnamed"
            if uid not in ids.get(platform, []):
                out.append(f"{platform}: route {name} sends someone other than the owner to {r.get('profile')}")
            elif platform in DM_CHAT_IS_USER and chat != uid:
                out.append(f"{platform}: route {name} also sends the owner's group messages to {r.get('profile')}")
    return out
