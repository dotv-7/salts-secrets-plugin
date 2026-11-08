"""Guest routing guards on both sides of Hermes' own ``gateway.profile_routes``.

``setup`` writes the routes: the owner's DM goes to the owner profile, everything else on the platform
to the public profile, so guests run through the native reply pipeline of the public profile. Under a
multiplexed gateway ``pre_gateway_dispatch`` fires in the profile a message was routed to, hence two hooks:

- public profile (``make_guest_hook``): per-guest hourly limit and bot authors, before the turn;
- owner profile (``make_hook``): whatever is still in the owner profile and is not the owner's own DM
  (the owner speaking in a Discord server, where no route can tell the owner's DM from a group; a
  platform without routes) is answered by the public profile over its local API and skipped.

The owner hook never raises. The gateway treats a raising hook as "dispatch normally", which would hand
the message to the owner agent, so every failure on that path still ends in ``skip``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .home import NAME, PUBLIC_KEY_ENV, current, data_dir

log = logging.getLogger(__name__)

NOTICE_UNAVAILABLE = "The guest entrance is temporarily unavailable. Please try again in a minute."
NOTICE_RATE = "You have reached the guest message limit for this hour. Please come back later."
NOTICE_TEXT_ONLY = "I can only read text here."
REPLY_MAX = 3500

Getter = Callable[[str, str], str]


def scoped_env(name: str, default: str = "") -> str:
    """Profile-scoped variable lookup; never reads another profile's value under multiplexing."""
    from agent.secret_scope import get_secret

    return get_secret(name, default) or default


@dataclass
class Settings:
    owners: set[str]
    public_profile: str = "public"
    rate_per_hour: int = 20
    api_base: str = "http://127.0.0.1:8642"
    env: Getter = scoped_env

    @property
    def public_key(self) -> str:
        return self.env(PUBLIC_KEY_ENV, "")


HEARTBEAT_FILE = "route_heartbeat.json"
HEARTBEAT_KEEP = 8


def process_start_time(pid: int) -> Optional[int]:
    """The gateway's own (pid, start_time) fingerprint from gateway/status.py, so ``check`` can compare."""
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(pid)
    except Exception:  # noqa: BLE001
        return None


def write_heartbeat(home=None, pid: Optional[int] = None, now: Optional[float] = None) -> dict:
    """Record that the routing hook is registered in this process. Entries are keyed by pid: CLI processes
    load plugins too, and ``check`` only trusts the entry whose pid and start time are the gateway's."""
    pid = pid or os.getpid()
    entry = {"pid": pid, "start_time": process_start_time(pid), "at": int(now or time.time())}
    path = data_dir(home or current()) / HEARTBEAT_FILE
    try:
        beats = json.loads(path.read_text(encoding="utf-8")).get("processes") or {}
    except (OSError, ValueError, AttributeError):
        beats = {}
    beats[str(pid)] = entry
    keep = dict(sorted(beats.items(), key=lambda kv: -int((kv[1] or {}).get("at") or 0))[:HEARTBEAT_KEEP])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{pid}")
    tmp.write_text(json.dumps({"processes": keep}, indent=1), encoding="utf-8")
    tmp.replace(path)
    return entry


def platform_name(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def is_owner_dm(event: Any, owners: set[str]) -> bool:
    src = getattr(event, "source", None)
    if src is None or getattr(src, "chat_type", "") != "dm":
        return False
    uid = str(getattr(src, "user_id", None) or getattr(event, "user_id", None) or "")
    return bool(uid) and f"{platform_name(src.platform)}:{uid}".lower() in owners


def guest_session_id(event: Any) -> str:
    src = event.source
    platform = platform_name(src.platform)
    if src.chat_type == "dm":
        return f"guest-{platform}-dm-{src.user_id}"
    return f"guest-{platform}-{src.chat_type}-{src.chat_id}"


def guest_text(event: Any) -> str:
    """Message text; in shared venues prefixed with the speaker so the public agent can tell them apart."""
    text = (getattr(event, "text", "") or "").strip()
    src = event.source
    if src.chat_type == "dm" or not text:
        return text
    who = getattr(src, "user_name", None) or getattr(event, "user_name", None) or src.user_id or "guest"
    return f"[{who}] {text}"


class RateLimiter:
    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        hits = self.hits[key]
        while hits and hits[0] < now - 3600:
            hits.popleft()
        if len(hits) >= self.per_hour:
            return False
        hits.append(now)
        return True


async def ask_public_profile(settings: Settings, session_id: str, text: str) -> str:
    """One turn against the public profile; Hermes keeps the conversation under ``session_id``."""
    import httpx

    url = f"{settings.api_base}/p/{settings.public_profile}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {settings.public_key}", "X-Hermes-Session-Id": session_id}
    payload = {"model": "hermes-agent", "messages": [{"role": "user", "content": text}]}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
    message = (data.get("choices") or [{}])[0].get("message") or {}
    return (message.get("content") or "").strip()


async def _reply(gateway: Any, event: Any, content: str) -> None:
    src = event.source
    delivery = getattr(gateway, "_delivery_adapter_for", None)
    adapter = delivery(src) if callable(delivery) else None
    if adapter is not None:
        await adapter.send(src.chat_id, content[:REPLY_MAX], reply_to=getattr(event, "message_id", None))
        return
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get(src.platform) or next(
        (a for key, a in adapters.items() if platform_name(key) == platform_name(src.platform)), None
    )
    if adapter is None:
        log.warning("%s: no adapter for %s, reply dropped", NAME, platform_name(src.platform))
        return
    await adapter.send(src.chat_id, content[:REPLY_MAX], reply_to=getattr(event, "message_id", None))


def _live_session(settings: Settings, session_id: str) -> str:
    from . import guests
    from .home import current

    return guests.live_session_id(current() / "profiles" / settings.public_profile, session_id)


def guest_key(src: Any) -> str:
    return f"{platform_name(src.platform)}:{src.user_id or src.chat_id}"


def make_guest_hook(limiter: RateLimiter):
    """Public profile: guards for guests the native routes delivered here. ``None`` lets the turn run."""

    async def hook(event: Any, gateway: Any = None, **_: Any) -> Optional[dict]:
        src = event.source
        if getattr(src, "is_bot", False):
            return {"action": "skip", "reason": f"{NAME}: bot author"}
        if limiter.allow(guest_key(src)):
            return None
        try:
            await _reply(gateway, event, NOTICE_RATE)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: could not send the rate notice: %s", NAME, exc)
        return {"action": "skip", "reason": f"{NAME}: rate limited"}

    return hook


def register_guest(ctx) -> None:
    rate = max(1, int(ctx.get_config("guest_rate_per_hour", default=20) or 20))
    ctx.register_hook("pre_gateway_dispatch", make_guest_hook(RateLimiter(rate)))


def make_hook(settings: Settings, limiter: RateLimiter, asker=ask_public_profile):
    def skip(reason: str) -> dict:
        return {"action": "skip", "reason": f"{NAME}: {reason}"}

    async def hook(event: Any, gateway: Any = None, **_: Any) -> Optional[dict]:
        try:
            if is_owner_dm(event, settings.owners):
                return None
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: could not classify message (%s); treating it as a guest", NAME, exc)
        try:
            src = event.source
            if getattr(src, "is_bot", False):
                return skip("bot author")
            text = guest_text(event)
            if not text:
                await _reply(gateway, event, NOTICE_TEXT_ONLY)
                return skip("no text")
            if not limiter.allow(guest_key(src)):
                await _reply(gateway, event, NOTICE_RATE)
                return skip("rate limited")
            if not settings.public_key:
                await _reply(gateway, event, NOTICE_UNAVAILABLE)
                return skip(f"{PUBLIC_KEY_ENV} is not set")
            reply = await asker(settings, _live_session(settings, guest_session_id(event)), text)
            await _reply(gateway, event, reply or "(no reply)")
            return skip("answered by the public profile")
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: guest path failed: %s", NAME, exc)
            try:
                await _reply(gateway, event, NOTICE_UNAVAILABLE)
            except Exception:  # noqa: BLE001
                pass
            return skip("failed closed")

    return hook


def build_settings(get_config: Callable[[str, Any], Any], env: Getter = scoped_env) -> Settings:
    owners = {str(o).strip().lower() for o in get_config("owners", []) or [] if str(o).strip()}
    host = env("API_SERVER_HOST", "127.0.0.1") or "127.0.0.1"
    port = env("API_SERVER_PORT", "8642") or "8642"
    return Settings(
        owners=owners,
        public_profile=get_config("public_profile", "public") or "public",
        rate_per_hour=max(1, int(get_config("guest_rate_per_hour", 20) or 20)),
        api_base=f"http://{host}:{port}",
        env=env,
    )


def register(ctx) -> None:
    settings = build_settings(lambda key, default: ctx.get_config(key, default=default))
    if not settings.owners:
        log.warning("%s: no owner identity configured; every platform message is treated as a guest", NAME)
    ctx.register_hook("pre_gateway_dispatch", make_hook(settings, RateLimiter(settings.rate_per_hour)))
    try:
        write_heartbeat()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: could not write the routing heartbeat (%s); check will report BROKEN", NAME, exc)
