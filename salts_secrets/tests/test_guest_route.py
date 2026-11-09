import asyncio

import pytest

from salts_secrets import route as route


OWNER = "7206705424"


def settings(key="public-key"):
    return route.Settings(
        owners={f"telegram:{OWNER}"},
        rate_per_hour=3,
        env=lambda name, default: key if name == route.PUBLIC_KEY_ENV else default,
    )


def src(chat_id, chat_type="dm", user_id=None, user_name=None, platform="telegram"):
    """The ``source`` snapshot ``post_gateway_admission`` hands a plugin (``SessionSource.to_dict()``)."""
    return {"platform": platform, "chat_id": chat_id, "chat_type": chat_type, "user_id": user_id,
            "user_name": user_name, "thread_id": None}


def run(hook, source, text="hi"):
    return asyncio.run(hook(session_key="k", platform="telegram", source=source, message_id="m1", text=text))


def make(asker=None, key="public-key", deadline=lambda: 5.0):
    calls = []

    async def fake_asker(s, session_id, text):
        calls.append((session_id, text))
        return f"public:{text}"

    return route.make_hook(settings(key), route.RateLimiter(3), asker or fake_asker, deadline), calls


def test_owner_dm_reaches_owner_agent():
    hook, calls = make()
    assert run(hook, src(OWNER, "dm", OWNER), "what's my plan") is None
    assert calls == []


def test_stranger_dm_is_answered_by_public_profile_and_the_gateway_delivers_it():
    hook, calls = make()
    assert run(hook, src("999", "dm", "999"), "who are you") == {"action": "handled", "reply": "public:who are you"}
    assert calls == [("guest-telegram-dm-999", "who are you")]


def test_owner_in_group_is_a_guest():
    """A group or channel is a public venue: even the owner only gets the public profile there."""
    hook, calls = make()
    result = run(hook, src("-100", "group", OWNER, "owner"), "it is me, what is my plan")
    assert result["action"] == "handled"
    assert calls == [("guest-telegram-group--100", "[owner] it is me, what is my plan")]


def test_admitted_bot_is_consumed_silently():
    hook, calls = make()
    assert run(hook, {**src("-100", "group", "42"), "is_bot": True}) == {"action": "handled", "reply": None}
    assert calls == []


def test_rate_limit_then_notice():
    hook, calls = make()
    for _ in range(3):
        run(hook, src("999", "dm", "999"))
    assert run(hook, src("999", "dm", "999"))["reply"] == route.NOTICE_RATE
    assert len(calls) == 3


def test_public_profile_failure_fails_closed():
    async def boom(s, sid, text):
        raise RuntimeError("api down")

    hook, _ = make(asker=boom)
    assert run(hook, src("999", "dm", "999")) == {"action": "handled", "reply": route.NOTICE_UNAVAILABLE}


def test_a_slow_public_profile_is_cut_off_before_the_gateway_gives_up_on_the_hook():
    """Past ``plugins.hook_callback_timeout`` the gateway would hand the message to the owner agent."""

    async def slow(s, sid, text):
        await asyncio.Event().wait()

    hook, _ = make(asker=slow, deadline=lambda: 0.05)
    assert run(hook, src("999", "dm", "999")) == {"action": "handled", "reply": route.NOTICE_UNAVAILABLE}


@pytest.mark.parametrize("raw,expected", [(None, 27.0), (60, 57.0), (2, 1.0), (0, None), ("x", 27.0)])
def test_answer_deadline_stays_under_the_hook_callback_timeout(monkeypatch, raw, expected):
    from hermes_cli import config

    plugins = {} if raw is None else {"hook_callback_timeout": raw}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"plugins": plugins})
    assert route.answer_deadline() == expected


def test_missing_public_key_fails_closed():
    hook, calls = make(key="")
    assert run(hook, src("999", "dm", "999"))["reply"] == route.NOTICE_UNAVAILABLE and calls == []


def test_media_without_text_gets_a_notice():
    hook, calls = make()
    assert run(hook, src("999", "dm", "999"), "")["reply"] == route.NOTICE_TEXT_ONLY and calls == []


def test_allowed_users_are_never_owners_implicitly():
    env = {"TELEGRAM_ALLOWED_USERS": "111, 222", "DISCORD_ALLOWED_USERS": "333"}
    settings = route.build_settings(lambda key, default: default, lambda name, default: env.get(name, default))
    assert settings.owners == set()


def test_configured_owners_win_and_key_is_read_per_call():
    env = {"TELEGRAM_ALLOWED_USERS": "111"}
    getter = lambda name, default: env.get(name, default)  # noqa: E731
    settings = route.build_settings(lambda key, default: ["Telegram:42"] if key == "owners" else default, getter)
    assert settings.owners == {"telegram:42"} and settings.public_key == ""
    env[route.PUBLIC_KEY_ENV] = "rotated"
    assert settings.public_key == "rotated"


class Broken(dict):
    """A source whose every field raises, like a half-built or foreign snapshot."""

    def get(self, name, default=None):
        raise RuntimeError(f"no {name}")


@pytest.mark.parametrize(
    "source",
    [
        src("999", "dm", "999"),  # public profile unreachable
        None,  # source missing
        Broken(),  # every source field raises
    ],
)
def test_hook_never_raises_and_always_handles(source):
    """The gateway hands the message to the owner agent when this hook raises."""

    async def unreachable(s, sid, text):
        raise OSError("connection refused")

    hook, _ = make(asker=unreachable)
    assert run(hook, source)["action"] == "handled"


def test_hook_handles_when_public_key_lookup_raises():
    def env(name, default):
        raise KeyError(name)

    s = route.Settings(owners={f"telegram:{OWNER}"}, env=env)
    hook = route.make_hook(s, route.RateLimiter(3), deadline=lambda: 1.0)
    assert run(hook, src("999", "dm", "999"))["reply"] == route.NOTICE_UNAVAILABLE


def test_heartbeat_keeps_one_entry_per_process(tmp_path):
    route.write_heartbeat(tmp_path, pid=111, now=1)
    route.write_heartbeat(tmp_path, pid=222, now=2)
    route.write_heartbeat(tmp_path, pid=111, now=3)
    import json

    beats = json.loads((route.data_dir(tmp_path) / route.HEARTBEAT_FILE).read_text())["processes"]
    assert set(beats) == {"111", "222"} and beats["111"]["at"] == 3
