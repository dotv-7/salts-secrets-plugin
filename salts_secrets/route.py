"""Guest routing guards on both sides of Hermes' own ``gateway.profile_routes``.

``setup`` writes the routes: the owner's DM goes to the owner profile, everything else on the platform
to the public profile, so guests run through the native reply pipeline of the public profile. Under a
multiplexed gateway the gateway hooks fire in the profile a message was routed to, hence two sides:

- public profile (``make_guest_hooks``): bot authors are dropped and a guest whose session a failed purge
  left behind is moved to a fresh session (``pre_gateway_dispatch``, which hands over the session store);
  after admission each guest gets an hourly limit, and a guest whose session could not be moved is refused
  (``post_gateway_admission``);
- owner profile (``make_hook``, ``post_gateway_admission``): whatever the gateway admitted into the owner
  profile that is not the owner's own DM (the owner speaking in a Discord server, where no route can tell the
  owner's DM from a group; an admitted sender on a platform without routes) is answered by the public profile
  over its local API, and the gateway delivers that reply. Webhook deliveries (``chat_type`` ``webhook``: the
  owner's own signed webhook routes, Microsoft Graph notifications) are not chat and pass to the owner agent.

``post_gateway_admission`` fails open: a callback that raises or outlives ``plugins.hook_callback_timeout``
hands the message to the owner agent. The owner hook therefore never raises and stops waiting for the public
profile before that timeout; every failure on that path still ends in ``handled`` with a notice.
"""

from __future__ import annotations

import asyncio
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
NOTICE_REVOKED_MID_TURN = (
    "The owner withdrew something while this reply was being written, so it was not sent. "
    "Please ask again."
)
NOTICE_REVOKE_PENDING = (
    "The owner withdrew something this conversation used and the cleanup is not finished. "
    "Please send your message again in a minute."
)
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


def field(src: Any, name: str) -> Any:
    """A message source field, from ``SessionSource`` (``pre_gateway_dispatch``) or its ``to_dict()``
    snapshot (``post_gateway_admission``)."""
    return src.get(name) if isinstance(src, dict) else getattr(src, name, None)


def is_owner_dm(src: Any, owners: set[str]) -> bool:
    if src is None or field(src, "chat_type") != "dm":
        return False
    uid = str(field(src, "user_id") or "")
    return bool(uid) and f"{platform_name(field(src, 'platform'))}:{uid}".lower() in owners


def is_webhook_delivery(src: Any) -> bool:
    """A delivery to one of the owner's webhook routes, which Hermes authenticated before admission."""
    return src is not None and field(src, "chat_type") == "webhook"


def guest_session_id(src: Any) -> str:
    platform = platform_name(field(src, "platform"))
    if field(src, "chat_type") == "dm":
        return f"guest-{platform}-dm-{field(src, 'user_id')}"
    return f"guest-{platform}-{field(src, 'chat_type')}-{field(src, 'chat_id')}"


def guest_text(text: str, src: Any) -> str:
    """Message text; in shared venues prefixed with the speaker so the public agent can tell them apart."""
    text = (text or "").strip()
    if field(src, "chat_type") == "dm" or not text:
        return text
    who = field(src, "user_name") or field(src, "user_id") or "guest"
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


def _live_session(settings: Settings, session_id: str) -> str:
    from . import guests
    from .home import current

    return guests.live_session_id(current() / "profiles" / settings.public_profile, session_id)


def guest_key(src: Any) -> str:
    return f"{platform_name(field(src, 'platform'))}:{field(src, 'user_id') or field(src, 'chat_id')}"


def handled(reply: Optional[str]) -> dict:
    """``post_gateway_admission``: the message is consumed; the gateway delivers ``reply`` (if any)."""
    return {"action": "handled", "reply": reply}


def leave_stale_session(session_store: Any) -> bool:
    """Move every guest off a session a failed purge left behind, before the gateway resumes it.

    Uses the session store the gateway hands ``pre_gateway_dispatch``: each stale session still routed to a
    chat is reset to a fresh one (a new session id, so the turn loads an empty transcript and the agent cache
    rebuilds). ``False`` when that cannot be settled (no store, reset refused) while a purge is incomplete:
    the caller then refuses the turn rather than risk serving the revoked transcript."""
    from . import guests

    stale = guests.stale_ids(current())
    if not stale:
        return True
    if session_store is None:
        return False
    try:
        for sid in stale:
            entry = session_store.lookup_by_session_id(sid)
            if entry is not None and session_store.reset_session(entry.session_key) is None:
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: cannot move guests off revoked sessions (%s)", NAME, exc)
        return False


def make_guest_hooks(limiter: RateLimiter) -> tuple[Callable, Callable]:
    """Public profile: ``(pre_gateway_dispatch, post_gateway_admission)`` guards for routed guests.

    Before admission only the session store is touched (no reply is sent); refusals are replies to admitted
    messages, which the gateway delivers. ``None`` lets the turn run."""
    unsettled: set[str] = set()

    async def before(event: Any, session_store: Any = None, **_: Any) -> Optional[dict]:
        src = event.source
        if field(src, "is_bot"):
            return {"action": "skip", "reason": f"{NAME}: bot author"}
        if leave_stale_session(session_store):
            unsettled.discard(guest_key(src))
        else:
            unsettled.add(guest_key(src))
        return None

    async def admitted(source: Optional[dict] = None, **_: Any) -> Optional[dict]:
        key = guest_key(source or {})
        if key in unsettled:
            unsettled.discard(key)
            return handled(NOTICE_REVOKE_PENDING)
        if not limiter.allow(key):
            return handled(NOTICE_RATE)
        return None

    return before, admitted


class InFlightRevoke:
    """A reply whose turn started before the owner revoked something is replaced by a notice.

    ``pre_llm_call`` notes when each guest turn started; ``transform_llm_output`` runs before the reply is
    stored or (for non-streamed replies) delivered, and compares that with the public profile's last
    revocation. Text a platform already streamed cannot be taken back; it is still kept out of the transcript."""

    def __init__(self, revoked_at: Callable[[], float] = lambda: 0.0, clock: Callable[[], float] = time.time):
        self.revoked_at, self.clock = revoked_at, clock
        self.started: dict[str, float] = {}

    def on_turn_start(self, session_id: str = "", **_: Any) -> None:
        self.started[str(session_id or "")] = self.clock()

    def on_reply(self, session_id: str = "", **_: Any) -> Optional[str]:
        started = self.started.pop(str(session_id or ""), None)
        if started is None:
            return None
        return NOTICE_REVOKED_MID_TURN if self.revoked_at() >= started else None


def register_guest(ctx) -> None:
    from . import guests

    rate = max(1, int(ctx.get_config("guest_rate_per_hour", default=20) or 20))
    before, admitted = make_guest_hooks(RateLimiter(rate))
    ctx.register_hook("pre_gateway_dispatch", before)
    ctx.register_hook("post_gateway_admission", admitted)
    inflight = InFlightRevoke(lambda: guests.revoked_at(current()))
    ctx.register_hook("pre_llm_call", inflight.on_turn_start)
    ctx.register_hook("transform_llm_output", inflight.on_reply)


HOOK_TIMEOUT_DEFAULT = 30.0
HOOK_TIMEOUT_MARGIN = 3.0


def answer_deadline() -> Optional[float]:
    """Seconds the owner hook waits for the public profile: just under ``plugins.hook_callback_timeout``,
    after which the gateway would give up on the hook and hand the message to the owner agent."""
    from hermes_cli.config import load_config_readonly

    raw = ((load_config_readonly() or {}).get("plugins") or {}).get("hook_callback_timeout")
    try:
        timeout = HOOK_TIMEOUT_DEFAULT if raw is None else float(raw)
    except (TypeError, ValueError):
        timeout = HOOK_TIMEOUT_DEFAULT
    return None if timeout <= 0 else max(1.0, timeout - HOOK_TIMEOUT_MARGIN)


def make_hook(settings: Settings, limiter: RateLimiter, asker=ask_public_profile,
              deadline: Callable[[], Optional[float]] = answer_deadline):
    """Owner profile, ``post_gateway_admission``: answer every admitted message that is neither the owner's DM
    nor a webhook delivery."""

    async def answer(source: Any, text: str) -> dict:
        if field(source, "is_bot"):
            return handled(None)
        text = guest_text(text, source)
        if not text:
            return handled(NOTICE_TEXT_ONLY)
        if not limiter.allow(guest_key(source)):
            return handled(NOTICE_RATE)
        if not settings.public_key:
            log.warning("%s: %s is not set; guest message refused", NAME, PUBLIC_KEY_ENV)
            return handled(NOTICE_UNAVAILABLE)
        reply = await asker(settings, _live_session(settings, guest_session_id(source)), text)
        return handled((reply or "(no reply)")[:REPLY_MAX])

    async def hook(source: Any = None, text: str = "", **_: Any) -> Optional[dict]:
        try:
            if is_owner_dm(source, settings.owners) or is_webhook_delivery(source):
                return None
        except Exception as exc:  # noqa: BLE001 - an unreadable source is a guest, never the owner
            log.warning("%s: could not classify message (%s); treating it as a guest", NAME, exc)
        try:
            return await asyncio.wait_for(answer(source, text), deadline())
        except Exception as exc:  # noqa: BLE001 - fail closed: a raising hook would reach the owner agent
            log.warning("%s: guest path failed: %s", NAME, type(exc).__name__)
            return handled(NOTICE_UNAVAILABLE)

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
    ctx.register_hook("post_gateway_admission", make_hook(settings, RateLimiter(settings.rate_per_hour)))
    try:
        write_heartbeat()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s: could not write the routing heartbeat (%s); check will report BROKEN", NAME, exc)
