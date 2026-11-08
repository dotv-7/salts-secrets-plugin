import json
from types import SimpleNamespace

import pytest

from salts_secrets import context, notes
from salts_secrets.home import data_dir

OWNER = SimpleNamespace(platform="telegram", user_id="42", chat_type="dm")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "owner"
    (h / "memories").mkdir(parents=True)
    (h / "memories/MEMORY.md").write_text("blog is lowtide.news", encoding="utf-8")
    (h / "config.yaml").write_text(
        "plugins:\n  entries:\n    salts-secrets:\n      settings:\n        owner_name: salt\n"
        f"        workspaces: ['{tmp_path / 'ws'}']\n", encoding="utf-8")
    monkeypatch.setattr("salts_secrets.guests.revoke_and_purge", lambda *a, **k: {"revoked": 0})
    return h


def run(cmd, text, source=OWNER, **kw):
    return cmd(text, source=source, session_id=kw.get("session_id", "s1"))


def test_agent_can_only_queue_and_c_never_queues(home):
    queued = notes.propose(home, "Salt has a cat; she keeps its name to herself.")
    assert queued["status"] == "pending" and queued["next"] == f"/salt approve {queued['id']}"
    refused = notes.propose(home, "My Wi-Fi password is hunter2")
    assert refused["status"] == "refused" and refused["rules_hit"]
    assert [n["id"] for n in notes.pending(home)] == [queued["id"]]
    assert not (data_dir(home) / context.NOTES_FILE).exists()
    assert "cat" not in json.dumps(context.read_projection(home / "profiles/public"))


def test_only_the_owner_dm_approves(home):
    nid = notes.propose(home, "Salt is writing issue 9 about bridges.")["id"]
    cmd = notes.make_command({"telegram:42"}, lambda: home)
    for source, sid in [(SimpleNamespace(platform="telegram", user_id="7", chat_type="dm"), "s"),
                        (SimpleNamespace(platform="telegram", user_id="42", chat_type="group"), "s"),
                        (OWNER, "salt-v-abc"), (None, "s")]:
        assert run(cmd, f"approve {nid}", source, session_id=sid).startswith("Only the owner")
    assert notes.pending(home)
    assert run(cmd, f"approve {nid}").startswith(f"Published note {nid}")
    proj = context.read_projection(home / "profiles/public")
    assert proj["facts"] == ["Salt is writing issue 9 about bridges."]
    log = context.read_approval_log(home)[-1]
    assert log["decided_via"] == "chat" and log["decided_by"] == "owner" and log["decision"] == "public"
    assert notes.pending(home) == []


def test_approving_a_note_keeps_earlier_rewrites(home):
    items = context.scan(home, [])
    mem = next(i for i in items if i.kind == "memory")
    context.approve(home, items, {mem.id}, {mem.id: "Salt writes a newsletter."})
    nid = notes.propose(home, "Salt answers mail on Fridays.")["id"]
    assert notes.approve(home, nid)["ok"]
    facts = context.read_projection(home / "profiles/public")["facts"]
    assert set(facts) == {"Salt writes a newsletter.", "Salt answers mail on Fridays."}


def test_drop_and_listing(home):
    cmd = notes.make_command({"telegram:42"}, lambda: home)
    assert run(cmd, "").startswith("No notes waiting")
    nid = notes.propose(home, "Salt is on holiday in May.")["id"]
    assert nid in run(cmd, "pending")
    assert run(cmd, f"drop {nid}") == f"Dropped note {nid}."
    assert notes.pending(home) == []
