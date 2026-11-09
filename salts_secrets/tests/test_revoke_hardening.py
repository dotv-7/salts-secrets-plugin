"""Revocation at scale and under failure: every session is reached, a crashed delete leaves guests moved,
a failed revoke is reported by ``sync``, and a reply written across a revocation is not delivered."""

import argparse
import sqlite3
import subprocess
import sys

from salts_secrets import cli, guests, route
from salts_secrets import context as core


def _store(home, n):
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "state.db")
    con.executescript("""
    create table sessions (id text primary key, source text not null, user_id text, display_name text, started_at real not null);
    create table messages (id integer primary key autoincrement, session_id text not null, role text not null, content text,
        tool_call_id text, timestamp real not null, active integer not null default 1);
    """)
    for i in range(n):
        con.execute("insert into sessions values (?,?,?,?,?)", (f"s{i}", "telegram", "", "", 1_700_000_000.0 + i))
        con.execute("insert into messages (session_id, role, content, tool_call_id, timestamp) values (?,?,?,?,?)",
                    (f"s{i}", "user", "hi", None, 1_700_000_000.0 + i))
    con.commit()
    con.close()


def _deleting_hermes(tmp_path):
    fake = tmp_path / "hermes"
    fake.write_text(
        f"#!{sys.executable}\nimport os, sqlite3, sys\n"
        "con = sqlite3.connect(os.path.join(os.environ['HERMES_HOME'], 'state.db'))\n"
        "con.execute('delete from messages where session_id = ?', (sys.argv[3],))\n"
        "con.execute('delete from sessions where id = ?', (sys.argv[3],))\ncon.commit()\n"
    )
    fake.chmod(0o755)
    return str(fake)


def _count(home):
    con = sqlite3.connect(home / "state.db")
    try:
        return con.execute("select count(*) from sessions").fetchone()[0]
    finally:
        con.close()


def test_purge_reaches_every_session_past_the_old_500_cap(tmp_path):
    pub = tmp_path / "public"
    _store(pub, 501)
    assert guests.purge(pub, hermes_bin=_deleting_hermes(tmp_path)) == {"deleted": 501, "failed": 0}
    assert _count(pub) == 0 and guests.stale_ids(pub) == set()


def test_archive_keeps_every_session(tmp_path):
    pub, owner = tmp_path / "public", tmp_path / "owner"
    _store(pub, 501)
    assert guests.archive(owner, pub) == 501


def test_a_delete_that_times_out_leaves_its_guest_moved(tmp_path, monkeypatch):
    pub = tmp_path / "public"
    _store(pub, 2)

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 120)

    monkeypatch.setattr(guests.subprocess, "run", hang)
    assert guests.purge(pub, hermes_bin="/bin/true") == {"deleted": 0, "failed": 2}
    assert guests.stale_ids(pub) == {"s0", "s1"}
    assert guests.live_session_id(pub, "s0") != "s0"


def test_a_crash_mid_purge_leaves_the_rest_stale(tmp_path, monkeypatch):
    pub = tmp_path / "public"
    _store(pub, 3)
    real = guests._delete
    done = []

    def crash_after_one(hermes, sid, env):
        if done:
            raise KeyboardInterrupt
        done.append(sid)
        return real(hermes, sid, env)

    monkeypatch.setattr(guests, "_delete", crash_after_one)
    try:
        guests.purge(pub, hermes_bin=_deleting_hermes(tmp_path))
    except KeyboardInterrupt:
        pass
    assert len(guests.stale_ids(pub)) == 2 and done[0] not in guests.stale_ids(pub)


def test_sync_exits_nonzero_when_a_revoke_is_incomplete(tmp_path, monkeypatch, capsys):
    owner, pub = tmp_path / "owner", tmp_path / "owner/profiles/public"
    (owner / "memories").mkdir(parents=True)
    (owner / "memories/MEMORY.md").write_text("Salt reads attestations date first", encoding="utf-8")
    pub.mkdir(parents=True)
    monkeypatch.setattr(cli, "current", lambda: owner)
    items = core.scan(owner, [])
    core.save_allowlist(owner, items, [items[0].id])
    assert cli.cmd_sync(argparse.Namespace(public=None)) == 0
    _store(pub, 1)
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setattr(guests, "find_hermes", lambda: str(broken))
    core.save_allowlist(owner, items, [])
    assert cli.cmd_sync(argparse.Namespace(public=None)) == 1
    assert "REVOKE INCOMPLETE" in capsys.readouterr().out


def test_revoke_records_the_time_guests_lost_something(tmp_path):
    owner, pub = tmp_path / "owner", tmp_path / "public"
    _store(pub, 1)
    before = {"facts": ["a fact"]}
    assert guests.revoked_at(pub) == 0.0
    guests.revoke_and_purge(owner, pub, before, before, hermes_bin=_deleting_hermes(tmp_path))
    assert guests.revoked_at(pub) == 0.0
    guests.revoke_and_purge(owner, pub, before, {"facts": []}, hermes_bin=_deleting_hermes(tmp_path))
    assert guests.revoked_at(pub) > 0


def test_a_reply_written_across_a_revocation_is_withheld():
    clock, revoked = [100.0], [0.0]
    inflight = route.InFlightRevoke(lambda: revoked[0], lambda: clock[0])
    inflight.on_turn_start(session_id="g1")
    assert inflight.on_reply(session_id="g1", response_text="fine") is None
    inflight.on_turn_start(session_id="g1")
    revoked[0], clock[0] = 101.0, 102.0
    assert inflight.on_reply(session_id="g1", response_text="the revoked fact") == route.NOTICE_REVOKED_MID_TURN
    inflight.on_turn_start(session_id="g1")
    assert inflight.on_reply(session_id="g1", response_text="after the revoke") is None


def test_guest_role_registers_the_reply_check():
    hooks = []
    ctx = type("Ctx", (), {"register_hook": lambda self, n, f, **_: hooks.append(n),
                           "get_config": lambda self, k, default=None: default})()
    route.register_guest(ctx)
    assert {"pre_gateway_dispatch", "post_gateway_admission", "pre_llm_call", "transform_llm_output"} <= set(hooks)
