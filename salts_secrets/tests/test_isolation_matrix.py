"""Isolation matrix: every ingress (Telegram, Discord, the web) x owner/stranger x DM/group, through the real
gateway routing and authorization, then each probe a guest could use to reach the owner's memory, sessions,
files or unpublished items. One owner home is published through ``hermes salts-secrets sync``."""

import argparse
import asyncio
import sqlite3
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from demo.chat import server as web
from salts_secrets import _yaml as yaml
from salts_secrets import cli, gate, public_context, route, routes, tiers
from salts_secrets import context as core

OWNERS = ["telegram:42", "discord:7"]
PUBLISHED = "Salt runs bridge checks before every trade."
UNPUBLISHED = "Salt lives in Lisbon with her cat Miso."
SESSION_SECRET = "the router password is on the fridge"
FILE_SECRET = "seed words are in the blue notebook"
OWNER_TOOLSETS = {"memory", "session_search", "file", "terminal", "code_execution", "skills", "delegation",
                  "cronjob", "messaging", "browser", "todo", "kanban"}
OWNER_TOOLS = ["memory", "session_search", "read_file", "search_files", "write_file", "terminal",
               "execute_code", "skill_view", "send_message", "delegate_task"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Owner (default) and public profiles as setup + sync leave them, served by one multiplexed runner."""
    from agent import secret_scope
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.pairing import PairingStore
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.profile_routing import parse_profile_routes
    from gateway.run import GatewayRunner

    owner = tmp_path / "hh"
    pub = owner / "profiles" / "public"
    (owner / "memories").mkdir(parents=True)
    (owner / "memories/MEMORY.md").write_text(f"{PUBLISHED}\n§\n{UNPUBLISHED}\n", encoding="utf-8")
    (owner / "notes.md").write_text(FILE_SECRET, encoding="utf-8")
    con = sqlite3.connect(owner / "state.db")
    con.execute("create table messages (session_id text, role text, content text)")
    con.execute("insert into messages values ('o1', 'user', ?)", (SESSION_SECRET,))
    con.commit()
    con.close()
    (owner / ".env").write_text("TELEGRAM_ALLOWED_USERS=42\nDISCORD_ALLOWED_USERS=7\n"
                                "TELEGRAM_ALLOW_ALL_USERS=true\nDISCORD_ALLOW_ALL_USERS=true\n")
    (pub / "plugins/salts-secrets").mkdir(parents=True)
    (pub / "plugins/salts-secrets/__init__.py").write_text("")
    (pub / ".env").write_text("")
    (pub / "config.yaml").write_text((tiers.Path(cli.__file__).parent / "profile/config.yaml").read_text())
    monkeypatch.setattr(cli, "current", lambda: owner)
    items = core.scan(owner, [])
    core.save_allowlist(owner, items, [i.id for i in items if i.text.strip() == PUBLISHED])
    cli.cmd_sync(argparse.Namespace(public=None))
    tiers.sync(owner, {"guest": pub})

    monkeypatch.setenv("HERMES_HOME", str(owner))
    for key in ("TELEGRAM_ALLOWED_USERS", "DISCORD_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS",
                "DISCORD_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS", "GATEWAY_ALLOWED_USERS"):
        monkeypatch.delenv(key, raising=False)

    class Stub(BasePlatformAdapter):
        pass

    Stub.__abstractmethods__ = frozenset()
    prev = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {p: PlatformConfig(enabled=True, extra={}) for p in (Platform.TELEGRAM, Platform.DISCORD)}
    runner.config.profile_routes = parse_profile_routes(routes.guest_routes(OWNERS, "default", "public"))
    runner.pairing_store, runner.pairing_stores = PairingStore(profile="default"), {}
    runner._primary_profile_name = "default"
    adapters = {}
    for platform in (Platform.TELEGRAM, Platform.DISCORD):
        adapter = Stub.__new__(Stub)
        adapter.platform, adapter.gateway_runner = platform, runner
        adapter._pending_messages, adapter._active_sessions = {}, {}
        adapters[platform.value] = adapter
    runner.adapters = {a.platform: a for a in adapters.values()}
    runner._profile_adapters = {"public": {}}
    served = [("default", owner), ("public", pub)]
    with patch("hermes_cli.profiles.profiles_to_serve", return_value=served), \
            patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda n: owner / "profiles" / n), \
            patch("hermes_cli.profiles.profile_exists", return_value=True):
        yield SimpleNamespace(runner=runner, adapters=adapters, owner=owner, pub=pub)
    secret_scope.set_multiplex_active(prev)


def _land(world, platform, chat_id, chat_type, user_id, guild_id=None):
    """Profile home the gateway runs this message's hooks and turn under, after routing and admission."""
    from hermes_constants import get_hermes_home

    adapter = world.adapters[platform]
    extra = {"guild_id": guild_id} if guild_id else {}
    source = adapter.build_source(chat_id=chat_id, chat_type=chat_type, user_id=user_id, **extra)
    source._transport_adapter_ref = weakref.ref(adapter)
    assert world.runner._is_user_authorized_for_source(source) is True
    seen = {}

    async def handle(event):
        seen["home"] = get_hermes_home().resolve()

    world.runner._handle_message = handle
    asyncio.run(world.runner._make_default_profile_message_handler()(SimpleNamespace(source=source, text="hi")))
    return seen["home"], source


def _guest_probes(world, platform_key):
    """Everything a guest on the public profile could use to reach the owner, each refused."""
    from hermes_cli.tools_config import _get_platform_tools

    cfg = yaml.safe_load((world.pub / "config.yaml").read_text(encoding="utf-8"))
    toolsets = _get_platform_tools(cfg, platform_key)
    assert toolsets and not toolsets & OWNER_TOOLSETS, toolsets
    for tool in OWNER_TOOLS:
        assert gate.check(tool, {}, world.pub), tool
    for path in (world.owner / "memories/MEMORY.md", world.owner / "notes.md", world.owner / "state.db"):
        assert gate.check("vision_analyze", {"image_url": str(path)}, world.pub)
        assert gate.check("web_extract", {"urls": [f"file://{path}"]}, world.pub)
    assert cfg["memory"]["memory_enabled"] is False and cfg["memory"]["provider"] == ""
    assert cfg["tools"]["tool_search"]["enabled"] == "off"
    projection = public_context.load_projection(world.pub)
    section = public_context.render_section(projection)
    assert PUBLISHED in section and UNPUBLISHED not in section
    for query in ("Lisbon", "router password", "seed words"):
        refused = ("No public documents.", "No public document matches that query.")
        assert public_context.lookup(projection, query) in refused
    for path in world.pub.rglob("*"):
        if path.is_file():
            blob = path.read_bytes()
            for secret in (UNPUBLISHED, SESSION_SECRET, FILE_SECRET):
                assert secret.encode() not in blob, (path, secret)


MATRIX = [
    # ingress, actor, platform, chat_id, chat_type, user_id, guild_id, lands in
    ("telegram DM", "owner", "telegram", "42", "dm", "42", None, "owner"),
    ("telegram group", "owner", "telegram", "-100500", "group", "42", None, "public"),
    ("telegram DM", "stranger", "telegram", "9", "dm", "9", None, "public"),
    ("telegram group", "stranger", "telegram", "-100500", "group", "9", None, "public"),
    ("discord DM", "owner", "discord", "8001", "dm", "7", None, "owner"),
    ("discord server", "owner", "discord", "555", "group", "7", "1", "owner hook -> public"),
    ("discord DM", "stranger", "discord", "8002", "dm", "9", None, "public"),
    ("discord server", "stranger", "discord", "555", "group", "9", "1", "public"),
]


@pytest.mark.parametrize("ingress,actor,platform,chat_id,chat_type,user_id,guild_id,lands", MATRIX,
                         ids=[f"{m[0]}-{m[1]}" for m in MATRIX])
def test_matrix(world, ingress, actor, platform, chat_id, chat_type, user_id, guild_id, lands):
    home, source = _land(world, platform, chat_id, chat_type, user_id, guild_id)
    if lands == "public":
        assert home == world.pub.resolve()
        _guest_probes(world, platform)
        return
    assert home == world.owner.resolve()
    if lands == "owner":
        return
    # The route cannot tell the owner's Discord DM from a server; the owner hook withholds it.
    asked = []

    async def asker(settings, session_id, text):
        asked.append(session_id)
        return "public reply"

    from gateway.run_inbound_consumer import run_post_admission_hook
    from hermes_cli.plugins import get_plugin_manager

    settings = route.Settings(owners=set(OWNERS), env=lambda n, d: "k" * 32 if n == route.PUBLIC_KEY_ENV else d)
    manager = get_plugin_manager()
    manager._discovered = True
    manager._hooks.setdefault("post_gateway_admission", []).append(
        route.make_hook(settings, route.RateLimiter(20), asker, deadline=lambda: 5.0))
    event = SimpleNamespace(text="hi", message_id="m")
    assert asyncio.run(run_post_admission_hook(world.runner, event, source, "k")) == (True, "public reply")
    assert asked
    _guest_probes(world, "api_server")


def test_web_guests_use_the_public_profile_api(world):
    """The web chat sends guests to /p/<public>/v1, i.e. the public profile's api_server turn."""
    s = web.Settings({"SALT_OWNER_HOME": str(world.owner)})
    assert s.tier_profiles["guest"] == "public" and s.public_home == world.pub
    _guest_probes(world, "api_server")
