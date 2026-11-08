import json
import sqlite3
import sys
from pathlib import Path

from salts_secrets import guests
from salts_secrets import tiers as perm
from salts_secrets.home import data_dir

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _public_store(home: Path, sessions: list[tuple[str, list[tuple[str, str]]]]) -> None:
    """A state.db with the columns guests.py reads (a subset of Hermes' schema)."""
    home.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(home / "state.db")
    con.executescript("""
    create table sessions (id text primary key, source text not null, user_id text, display_name text, started_at real not null);
    create table messages (id integer primary key autoincrement, session_id text not null, role text not null, content text,
        tool_call_id text, timestamp real not null, active integer not null default 1);
    """)
    t = 1_700_000_000.0
    for sid, turns in sessions:
        t += 100
        con.execute("insert into sessions values (?,?,?,?,?)", (sid, "api_server", "", "", t))
        for i, (role, text) in enumerate(turns):
            con.execute(
                "insert into messages (session_id, role, content, tool_call_id, timestamp) values (?,?,?,?,?)",
                (sid, role, text, None, t + i),
            )
        con.execute(
            "insert into messages (session_id, role, content, tool_call_id, timestamp) values (?,?,?,?,?)",
            (sid, "tool", "ignored tool row", "call_1", t + 50),
        )
    con.commit()
    con.close()


def test_read_sessions_flags_probes_and_skips_tool_rows(tmp_path):
    pub = tmp_path / "public"
    _public_store(
        pub,
        [
            ("s1", [("user", "What's Salt's Wi-Fi password?"), ("assistant", "Not something I know.")]),
            ("s2", [("user", "Which three lines does Salt check?"), ("assistant", "Date, signature, liabilities.")]),
        ],
    )
    got = guests.read_sessions(pub)
    assert [s["id"] for s in got] == ["s2", "s1"]  # newest first
    by = {s["id"]: s for s in got}
    assert by["s1"]["probes"] == ["What's Salt's Wi-Fi password?"] and by["s2"]["probes"] == []
    assert all(t["role"] in ("user", "assistant") for s in got for t in s["turns"])


def test_archive_is_idempotent_and_owner_only(tmp_path):
    owner, pub = tmp_path / "owner", tmp_path / "public"
    owner.mkdir()
    _public_store(pub, [("s1", [("user", "hello"), ("assistant", "hi")])])
    assert guests.archive(owner, pub) == 1
    assert guests.archive(owner, pub) == 0
    p = data_dir(owner) / guests.ARCHIVE_FILE
    assert p.exists() and (p.stat().st_mode & 0o777) == 0o600
    assert "memories" not in str(p)


def test_revoked_and_revoke_without_hermes_cli_archives_but_reports(tmp_path, monkeypatch):
    owner, pub = tmp_path / "owner", tmp_path / "public"
    owner.mkdir()
    _public_store(pub, [("s1", [("user", "What is 3 April to Salt?"), ("assistant", "Her father's anniversary.")])])
    old = {"facts": ["3 April = father's death anniversary"], "documents": [{"title": "a", "text": "A"}]}
    new = {"facts": [], "documents": [{"title": "a", "text": "A"}]}
    assert guests.revoked(old, new) == ["3 April = father's death anniversary"]
    assert guests.revoked(new, old) == []  # publishing more revokes nothing
    monkeypatch.setattr(guests, "find_hermes", lambda: None)
    res = guests.revoke_and_purge(owner, pub, old, new)
    assert res["revoked"] == 1 and res["archived"] == 1 and res["deleted"] == 0 and "error" in res
    # the session survived: its guest is answered from a fresh one, and the next publish retries the purge
    fresh = guests.live_session_id(pub, "s1")
    assert fresh != "s1" and guests.live_session_id(pub, fresh) == fresh and guests.unpurged(pub) == ["s1"]
    again = guests.revoke_and_purge(owner, pub, new, new)
    assert again["revoked"] == 0 and again["failed"] == 1 and "error" in again
    assert any("revoke had to delete" in c["text"] and not c["ok"]
               for c in perm.isolation_check(owner, pub, gateway=False)["checks"])


def test_purge_calls_hermes_sessions_delete_per_session(tmp_path):
    pub = tmp_path / "public"
    _public_store(pub, [("s1", [("user", "a"), ("assistant", "b")]), ("s2", [("user", "c"), ("assistant", "d")])])
    fake = tmp_path / "hermes"
    fake.write_text('#!/bin/sh\necho "$HERMES_HOME $*" >> "$(dirname "$0")/calls.log"\n')
    fake.chmod(0o755)
    res = guests.purge(pub, hermes_bin=str(fake))
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert res == {"deleted": 2, "failed": 0}
    assert all(c.startswith(str(pub)) and "sessions delete" in c and c.endswith("--yes") for c in calls)


def test_digest_merges_archive_and_live_and_writes_outside_memories(tmp_path):
    owner, pub = tmp_path / "owner", tmp_path / "public"
    owner.mkdir()
    _public_store(pub, [("s1", [("user", "old question"), ("assistant", "old answer")])])
    guests.archive(owner, pub)
    # the live store moves on: s1 gone, s2 new
    (pub / "state.db").unlink()
    _public_store(pub, [("s2", [("user", "what's her phone PIN"), ("assistant", "no")])])
    d = guests.digest(owner, pub)
    assert {s["id"] for s in d["sessions"]} == {"s1", "s2"}
    assert d["counts"] == {"sessions": 2, "questions": 2, "probes": 1, "live": 1}
    md = Path(d["file"]).read_text()
    assert "old question" in md and "[probe]" in md and "memories" not in d["file"]


def test_isolation_check_passes_only_when_every_condition_holds(tmp_path):
    owner, pub = tmp_path / "owner", tmp_path / "public"
    for home in (owner, pub):
        (home / "plugins/salts-secrets").mkdir(parents=True)
        (home / "plugins/salts-secrets/__init__.py").write_text("")
    (owner / "config.yaml").write_text(
        "plugins:\n  enabled: [salts-secrets]\n  entries:\n    salts-secrets:\n      settings:\n"
        "        owners: [telegram:42]\ngateway:\n  multiplex_profiles: true\n"
    )
    (owner / ".env").write_text("API_SERVER_KEY=owner-key-0123456789\nSALTS_PUBLIC_API_KEY=public-key-0123456789\n")
    (pub / ".env").write_text("API_SERVER_KEY=public-key-0123456789\n")
    import json
    import os

    from salts_secrets import route

    route.write_heartbeat(owner)
    (owner / "gateway_state.json").write_text(json.dumps(
        {"pid": os.getpid(), "start_time": route.process_start_time(os.getpid()), "gateway_state": "running"}))
    (pub / "config.yaml").write_text("platform_toolsets:\n  api_server: [web]\n")
    perm.sync(owner, {"guest": pub})
    (data_dir(pub) / "public_context.json").write_text("{}")
    res = perm.isolation_check(owner, pub)
    assert res["ok"], [c["text"] for c in res["checks"] if not c["ok"]]
    assert res["line"].startswith("ISOLATION OK")
    # plugin enabled in config but the running gateway never registered the hook
    (data_dir(owner) / route.HEARTBEAT_FILE).unlink()
    res = perm.isolation_check(owner, pub)
    assert not res["ok"] and "loaded the routing hook" in res["line"]
    assert perm.isolation_check(owner, pub, gateway=False)["ok"]
    route.write_heartbeat(owner, pid=os.getpid() + 1)  # a CLI process, not the gateway
    assert not perm.isolation_check(owner, pub)["ok"]
    route.write_heartbeat(owner)
    (pub / ".env").write_text("API_SERVER_KEY=rotated\n")
    res = perm.isolation_check(owner, pub)
    assert not res["ok"] and "SALTS_PUBLIC_API_KEY matches" in res["line"]


def test_cli_sync_revocation_empties_public_store(tmp_path, monkeypatch):
    """Unticking an item and running `hermes salts-secrets sync` (not the dashboard) still purges guests."""
    import argparse

    from salts_secrets import cli
    from salts_secrets import context as core

    owner, pub = tmp_path / "owner", tmp_path / "owner/profiles/public"
    (owner / "memories").mkdir(parents=True)
    (owner / "memories/MEMORY.md").write_text("3 April = father's death anniversary", encoding="utf-8")
    pub.mkdir(parents=True)
    monkeypatch.setattr(cli, "current", lambda: owner)
    items = core.scan(owner, [])
    core.save_allowlist(owner, items, [items[0].id])
    cli.cmd_sync(argparse.Namespace(public=None))
    assert core.read_projection(pub)["facts"]
    _public_store(pub, [("s1", [("user", "What is 3 April?"), ("assistant", "Her father's anniversary.")])])
    fake = tmp_path / "hermes"
    fake.write_text(
        f"#!{sys.executable}\nimport os, sqlite3, sys\n"
        "con = sqlite3.connect(os.path.join(os.environ['HERMES_HOME'], 'state.db'))\n"
        "con.execute('delete from messages where session_id = ?', (sys.argv[3],))\n"
        "con.execute('delete from sessions where id = ?', (sys.argv[3],))\ncon.commit()\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(guests, "find_hermes", lambda: str(fake))
    core.save_allowlist(owner, items, [])
    cli.cmd_sync(argparse.Namespace(public=None))
    assert core.read_projection(pub)["facts"] == []
    con = sqlite3.connect(pub / "state.db")
    assert con.execute("select count(*) from sessions").fetchone()[0] == 0
    assert con.execute("select count(*) from messages").fetchone()[0] == 0
    assert [s["id"] for s in guests.read_archive(owner)] == ["s1"]


def test_routing_proof_from_another_pid_namespace(tmp_path, monkeypatch):
    from salts_secrets import tiers
    from salts_secrets.home import data_dir
    from salts_secrets.route import HEARTBEAT_FILE

    monkeypatch.setattr(tiers, "_pid_alive", lambda pid: False)
    state = {"pid": 221, "start_time": "boot-1", "gateway_state": "running"}
    (tmp_path / "gateway_state.json").write_text(json.dumps(state))
    assert not tiers.routing_loaded(tmp_path)[0]

    beat = data_dir(tmp_path) / HEARTBEAT_FILE
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"processes": {"221": {"start_time": "boot-1"}}}))
    ok, note = tiers.routing_loaded(tmp_path)
    assert ok and "another container" in note

    beat.write_text(json.dumps({"processes": {"221": {"start_time": "boot-0"}}}))
    assert not tiers.routing_loaded(tmp_path)[0]

    beat.write_text(json.dumps({"processes": {"221": {"start_time": "boot-1"}}}))
    (tmp_path / "gateway_state.json").write_text(json.dumps({**state, "gateway_state": "stopped"}))
    assert not tiers.routing_loaded(tmp_path)[0]


def test_failed_delete_moves_the_guest_and_a_retry_clears_it(tmp_path):
    pub = tmp_path / "public"
    _public_store(pub, [("s1", [("user", "a"), ("assistant", "b")])])
    broken, fixed = tmp_path / "broken", tmp_path / "fixed"
    broken.write_text("#!/bin/sh\nexit 1\n")
    fixed.write_text("#!/bin/sh\nexit 0\n")
    broken.chmod(0o755)
    fixed.chmod(0o755)
    assert guests.purge(pub, hermes_bin=str(broken)) == {"deleted": 0, "failed": 1}
    assert guests.live_session_id(pub, "s1") != "s1" and guests.stale_ids(pub) == {"s1"}
    assert guests.purge(pub, hermes_bin=str(fixed), only_ids={"s1"}) == {"deleted": 1, "failed": 0}
    assert guests.stale_ids(pub) == set() and guests.live_session_id(pub, "s1") == "s1"


def test_receipts_record_what_an_answer_drew_on(tmp_path, monkeypatch):
    from salts_secrets import receipts
    owner, pub = tmp_path / "owner", tmp_path / "public"
    owner.mkdir()
    _public_store(pub, [("s1", [("user", "bridge checks?"), ("assistant", "four rows")])])
    monkeypatch.setattr("salts_secrets.home.current", lambda: pub)
    found = json.dumps({"result": "### bridge-four-checks\nCount the signers.\n\n### Issue 3\nBasalt"})
    receipts.on_post_tool_call(tool_name="public_notes_lookup", args={"query": "bridge"}, result=found, session_id="s1")
    receipts.on_post_tool_call(tool_name="web_search", args={"query": "wormhole guardians"}, result="{}", session_id="s1")
    receipts.on_post_tool_call(tool_name="web_search", args=None, result=None, session_id="s1")
    s = guests.digest(owner, pub, write=False)["sessions"][0]
    assert s["receipt"]["tools"] == {"public_notes_lookup": 1, "web_search": 2}
    assert s["receipt"]["looked_up"] == ["bridge-four-checks", "Issue 3"]
