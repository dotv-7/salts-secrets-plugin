"""Guests on Hermes' own profile_routes (#115642): the routes setup writes, their audit, the guest-side
hook, and the real gateway resolvers deciding profile, authorization and plugin scope per message."""

import asyncio
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from salts_secrets import _yaml as yaml
from salts_secrets import cli, route, routes, tiers

OWNERS = ["telegram:42", "discord:7"]


def _route_for(raw, **source):
    from gateway.profile_routing import match_profile_route, parse_profile_routes

    hit = match_profile_route(parse_profile_routes(raw), **source)
    return hit.profile if hit else None


def test_owner_dm_routes_to_owner_and_everyone_else_to_public():
    raw = routes.guest_routes(OWNERS, "default", "public")
    assert _route_for(raw, platform="telegram", chat_id="42", user_id="42") == "default"
    assert _route_for(raw, platform="telegram", chat_id="-100500", user_id="42") == "public"
    assert _route_for(raw, platform="telegram", chat_id="9", user_id="9") == "public"
    assert _route_for(raw, platform="discord", chat_id="555", user_id="9", guild_id="1") == "public"
    # Discord routes cannot tell the owner's DM from a server; the owner hook catches the server case.
    assert _route_for(raw, platform="discord", chat_id="555", user_id="7", guild_id="1") == "default"


def test_apply_keeps_foreign_routes_and_replaces_its_own():
    cfg = {"gateway": {"profile_routes": [{"name": "work", "platform": "slack", "chat_id": "C1", "profile": "work"}]}}
    routes.apply(cfg, ["telegram:1"], "default", "public")
    routes.apply(cfg, OWNERS, "default", "public")
    names = [r["name"] for r in cfg["gateway"]["profile_routes"]]
    assert names[0] == "work" and len(names) == 5 and not any("telegram-1" in n for n in names)


@pytest.mark.parametrize(
    "extra,needle",
    [
        ([{"name": "x", "platform": "telegram", "chat_id": "-100", "profile": "default"}], "other than the owner"),
        ([{"name": "x", "platform": "telegram", "user_id": "42", "profile": "default"}], "group messages"),
    ],
)
def test_problems_flag_routes_that_reach_the_owner_agent(extra, needle):
    cfg = {"gateway": {"profile_routes": routes.guest_routes(OWNERS, "default", "public") + extra}}
    found = routes.problems(cfg, OWNERS, "default", "public", {})
    assert len(found) == 1 and needle in found[0]


def test_problems_need_a_catch_all_wherever_the_bot_is_open():
    cfg = {"gateway": {"profile_routes": [r for r in routes.guest_routes(OWNERS, "default", "public")
                                          if r["platform"] != "discord" or r.get("user_id")]}}
    assert routes.problems(cfg, OWNERS, "default", "public", {}) == ["discord: no catch-all route to public"]
    found = routes.problems({}, ["telegram:42"], "default", "public", {"DISCORD_ALLOW_ALL_USERS": "true"})
    assert any("DISCORD_ALLOW_ALL_USERS is on without a catch-all" in p for p in found)
    assert routes.problems({"gateway": {"profile_routes": routes.guest_routes(OWNERS, "default", "public")}},
                           OWNERS, "default", "public", {"TELEGRAM_ALLOW_ALL_USERS": "true"}) == []


def test_a_discord_allowlist_without_star_keeps_strangers_from_the_catch_all():
    cfg = {"gateway": {"profile_routes": routes.guest_routes(OWNERS, "default", "public")}}
    found = routes.problems(cfg, OWNERS, "default", "public", {"DISCORD_ALLOWED_USERS": "7"})
    assert len(found) == 1 and "DISCORD_ALLOWED_USERS" in found[0]
    assert routes.problems(cfg, OWNERS, "default", "public", {"DISCORD_ALLOWED_USERS": "7,*"}) == []
    assert routes.open_allowlist("7, 8") == "7,8,*" and routes.open_allowlist("") == ""


def test_the_discord_adapter_drops_strangers_unless_its_allowlist_holds_star(monkeypatch):
    """Why setup opens DISCORD_ALLOWED_USERS: the adapter filters before routing and skips allow-all."""
    pytest.importorskip("discord")
    from plugins.platforms.discord.adapter import DiscordAdapter

    adapter = object.__new__(DiscordAdapter)
    adapter._allowed_role_ids = set()
    monkeypatch.setattr(DiscordAdapter, "_discord_allow_all_users", lambda self: True)
    adapter._allowed_user_ids = {"7"}
    assert adapter._is_allowed_user("7", is_dm=True) and not adapter._is_allowed_user("9", is_dm=True)
    adapter._allowed_user_ids = set(routes.open_allowlist("7").split(","))
    assert adapter._is_allowed_user("9", is_dm=True)


def test_setup_writes_routes_and_opens_the_routed_platforms(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    home.mkdir()
    calls = []

    def fake_hermes(*args, profile=None):
        calls.append((profile, args))
        if args[:2] == ("profile", "create"):
            (home / "profiles" / args[2]).mkdir(parents=True)

    monkeypatch.setattr(cli, "current", lambda: home)
    monkeypatch.setattr(cli, "_hermes", fake_hermes)
    monkeypatch.setattr(tiers, "profile_secrets", lambda h: {"DISCORD_ALLOWED_USERS": "7"} if h == home else {})
    import argparse

    cli.cmd_setup(argparse.Namespace(public=None, owner=OWNERS + ["slack:U1"], owner_name="salt", reset_public=False))
    cfg = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["gateway"]["profile_routes"] == routes.guest_routes(OWNERS, "default", "public")
    opened = {args[2] for _, args in calls if args[:2] == ("config", "set") and args[2].endswith("_ALLOW_ALL_USERS")}
    assert opened == {"TELEGRAM_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS"}
    assert (None, ("config", "set", "DISCORD_ALLOWED_USERS", "7,*")) in calls


def test_every_platform_toolset_is_pinned_to_the_guest_tier(tmp_path):
    from hermes_cli.tools_config import _get_platform_tools

    pub = tmp_path / "public"
    (pub / "plugins/salts-secrets").mkdir(parents=True)
    (pub / "plugins/salts-secrets/__init__.py").write_text("")
    (pub / "config.yaml").write_text("platform_toolsets:\n  api_server: [web]\n", encoding="utf-8")
    assert any("telegram" in p and "not pinned" in p for p in tiers.audit(pub, tiers.GUEST_TOOLSETS))
    (tmp_path / "owner").mkdir()
    tiers.sync(tmp_path / "owner", {"guest": pub})
    assert tiers.audit(pub, tiers.GUEST_TOOLSETS) == []
    cfg = yaml.safe_load((pub / "config.yaml").read_text(encoding="utf-8"))
    for platform in ("telegram", "discord", "api_server"):
        enabled = _get_platform_tools(cfg, platform)
        assert enabled and enabled <= set(tiers.GUEST_TOOLSETS), (platform, enabled)
        assert not enabled & {"terminal", "file", "session_search", "memory"}


def _guest_event(user="9", is_bot=False):
    src = SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=user, chat_type="dm",
                          user_id=user, is_bot=is_bot)
    return SimpleNamespace(text="hi", source=src, message_id="m")


def test_guest_hooks_limit_each_guest_after_admission_and_drop_bots():
    before, admitted = route.make_guest_hooks(route.RateLimiter(2))
    run = lambda user: asyncio.run(admitted(source={"platform": "telegram", "chat_id": user, "chat_type": "dm",
                                                     "user_id": user}))
    assert run("9") is None and run("9") is None
    assert run("9") == {"action": "handled", "reply": route.NOTICE_RATE}
    assert run("10") is None
    assert asyncio.run(before(_guest_event(user="11", is_bot=True)))["action"] == "skip"
    assert asyncio.run(before(_guest_event(user="12"))) is None


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    """A bare multiplexed runner serving the owner (default) and ``public`` with setup's routes."""
    from agent import secret_scope
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.pairing import PairingStore
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner

    class Stub(BasePlatformAdapter):
        pass

    Stub.__abstractmethods__ = frozenset()
    home = tmp_path / "hh"
    (home / "profiles" / "public").mkdir(parents=True)
    (home / ".env").write_text("TELEGRAM_ALLOWED_USERS=42\nTELEGRAM_ALLOW_ALL_USERS=true\n")
    (home / "profiles" / "public" / ".env").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("TELEGRAM_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS",
                "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)
    prev = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = parse_profile_routes(routes.guest_routes(["telegram:42"], "default", "public"))
    runner.pairing_store, runner.pairing_stores = PairingStore(profile="default"), {}
    runner._primary_profile_name = "default"
    primary = Stub.__new__(Stub)
    primary.platform, primary.gateway_runner = Platform.TELEGRAM, runner
    primary._pending_messages, primary._active_sessions = {}, {}
    runner.adapters, runner._profile_adapters = {Platform.TELEGRAM: primary}, {"public": {}}
    served = [("default", home), ("public", home / "profiles" / "public")]
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=served), \
            patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda n: home / "profiles" / n), \
            patch("hermes_cli.profiles.profile_exists", return_value=True):
        yield SimpleNamespace(runner=runner, home=home, primary=primary)
    secret_scope.set_multiplex_active(prev)


@pytest.mark.parametrize(
    "chat_id,chat_type,user_id,profile",
    [("42", "dm", "42", "default"), ("-100500", "group", "42", "public"), ("9", "dm", "9", "public"),
     ("-100500", "group", "9", "public")],
)
def test_real_gateway_routes_authorizes_and_scopes_each_message(gateway, chat_id, chat_type, user_id, profile):
    """Profile, admission and the plugin scope ``pre_gateway_dispatch`` fires in, as the gateway decides them."""
    from hermes_constants import get_hermes_home

    source = gateway.primary.build_source(chat_id=chat_id, chat_type=chat_type, user_id=user_id)
    assert (source.profile or "default") == profile
    source._transport_adapter_ref = weakref.ref(gateway.primary)
    assert gateway.runner._is_user_authorized_for_source(source) is True
    seen = {}

    async def handle(event):
        seen["home"] = get_hermes_home()

    gateway.runner._handle_message = handle
    asyncio.run(gateway.runner._make_default_profile_message_handler()(SimpleNamespace(source=source, text="hi")))
    expected = gateway.home if profile == "default" else gateway.home / "profiles" / "public"
    assert seen["home"].resolve() == expected.resolve()


def test_stranger_is_refused_while_the_owner_bot_is_closed(gateway):
    (gateway.home / ".env").write_text("TELEGRAM_ALLOWED_USERS=42\n")
    source = gateway.primary.build_source(chat_id="9", chat_type="dm", user_id="9")
    source._transport_adapter_ref = weakref.ref(gateway.primary)
    assert source.profile == "public" and gateway.runner._is_user_authorized_for_source(source) is False
