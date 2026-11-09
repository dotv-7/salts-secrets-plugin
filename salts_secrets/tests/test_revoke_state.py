"""Revocation under failure: a guest session a purge could not delete is never resumed, on either ingress.

The native path is driven through a real GatewayRunner and SessionStore (the store the gateway keys a
guest's conversation in); the loopback path through the owner hook's own session mapping."""

import asyncio
from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource

from salts_secrets import guests, route

SECRET = "the owner's contact is Marta, revoked"


def _failing_purge(public_home, sid):
    """What a failed ``hermes sessions delete`` leaves behind: the row stays, the session is marked stale."""
    stale = guests._stale(public_home)
    stale[sid] = f"{sid}-rdead"
    guests._save_stale(public_home, stale)


def _guest(chat_id="9"):
    return SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm", user_id=chat_id)


def _native(public_home):
    return GatewayRunner(GatewayConfig(sessions_dir=public_home / "sessions"))


def _turn(hooks, runner, source):
    """Both gateway hooks for one message: ``(pre_gateway_dispatch result, post_gateway_admission result)``."""
    before, admitted = hooks
    event = SimpleNamespace(source=source, text="what did we talk about?", message_id="m")
    first = asyncio.run(before(event, gateway=runner, session_store=runner.session_store if runner else None))
    return first, asyncio.run(admitted(source=source.to_dict(), text=event.text))


def test_native_guest_on_an_unpurged_session_gets_a_fresh_one(tmp_path, monkeypatch):
    pub = tmp_path / "public"
    monkeypatch.setenv("HERMES_HOME", str(pub))
    with _profile_runtime_scope(pub):
        runner = _native(pub)
        store = runner.session_store
        old = store.get_or_create_session(_guest()).session_id
        store.append_to_transcript(old, {"role": "assistant", "content": SECRET})
        _failing_purge(pub, old)

        hooks = route.make_guest_hooks(route.RateLimiter(20))
        assert _turn(hooks, runner, _guest()) == (None, None)
        entry = store.get_or_create_session(_guest())
        assert entry.session_id != old
        assert SECRET not in str(store.load_transcript(entry.session_id))

        other = store.get_or_create_session(_guest("10")).session_id
        assert _turn(hooks, runner, _guest("10")) == (None, None)
        assert store.get_or_create_session(_guest("10")).session_id == other


def test_stale_map_outlives_a_gateway_restart_and_concurrent_turns_share_one_fresh_session(tmp_path, monkeypatch):
    pub = tmp_path / "public"
    monkeypatch.setenv("HERMES_HOME", str(pub))
    with _profile_runtime_scope(pub):
        runner = _native(pub)
        old = runner.session_store.get_or_create_session(_guest()).session_id
        runner.session_store.append_to_transcript(old, {"role": "assistant", "content": SECRET})
        _failing_purge(pub, old)

        restarted = _native(pub)
        before, _ = route.make_guest_hooks(route.RateLimiter(20))
        event = SimpleNamespace(source=_guest(), text="again", message_id="m")

        async def both():
            return await asyncio.gather(*(before(event, gateway=restarted, session_store=restarted.session_store)
                                          for _ in range(2)))

        assert asyncio.run(both()) == [None, None]
        fresh = restarted.session_store.get_or_create_session(_guest()).session_id
        assert fresh != old and SECRET not in str(restarted.session_store.load_transcript(fresh))


def test_native_guest_is_refused_when_the_session_cannot_be_settled(tmp_path, monkeypatch):
    pub = tmp_path / "public"
    monkeypatch.setenv("HERMES_HOME", str(pub))
    _failing_purge(pub, "s-old")
    hooks = route.make_guest_hooks(route.RateLimiter(20))
    assert _turn(hooks, None, _guest()) == (None, {"action": "handled", "reply": route.NOTICE_REVOKE_PENDING})


def test_loopback_guest_on_an_unpurged_session_is_answered_in_a_fresh_one(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    pub = owner / "profiles" / "public"
    monkeypatch.setenv("HERMES_HOME", str(owner))
    source = {"platform": "discord", "chat_type": "group", "chat_id": "c1", "user_id": "5", "user_name": "x"}
    old = route.guest_session_id(source)
    _failing_purge(pub, old)
    asked = []

    async def asker(settings, session_id, text):
        asked.append(session_id)
        return "ok"

    settings = route.Settings(owners={"discord:7"}, env=lambda k, d="": "k" * 20)
    hook = route.make_hook(settings, route.RateLimiter(20), asker=asker, deadline=lambda: 5.0)
    assert asyncio.run(hook(source=source, text="what did we talk about?")) == {"action": "handled", "reply": "ok"}
    assert asked and asked[0] != old and asked[0] == guests.live_session_id(pub, old)
