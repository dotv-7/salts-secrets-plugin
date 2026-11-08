import json
from pathlib import Path

import pytest

from salts_secrets import context as core
from salts_secrets import public_context as plugin
from salts_secrets.home import data_dir

CANARY = "CANARY-PRIVATE-7f3a"


@pytest.fixture
def homes(tmp_path):
    owner, public, ws = tmp_path / "owner", tmp_path / "public", tmp_path / "ws"
    (owner / "memories").mkdir(parents=True)
    ws.mkdir()
    (owner / "memories/MEMORY.md").write_text(f"blog is lowtide.news\n§\nsecret {CANARY}", encoding="utf-8")
    (owner / "memories/USER.md").write_text("name is .v", encoding="utf-8")
    (ws / "bio.md").write_text(f"# Bio\n## School\nCS master\n## Love\n{CANARY}\n", encoding="utf-8")
    return owner, public, [ws]


def by_title(items, needle):
    return next(i for i in items if needle in i.title)


def test_scan_covers_sources(homes):
    owner, _, ws = homes
    kinds = {i.kind for i in core.scan(owner, ws)}
    assert {"memory", "user", "workspace"} <= kinds


def test_private_by_default(homes):
    owner, public, ws = homes
    proj = core.sync_public(owner, public, ws)
    assert proj["facts"] == [] and proj["documents"] == []
    assert CANARY not in (data_dir(public) / core.PROJECTION_FILE).read_text()


def test_only_approved_items_projected_and_originals_untouched(homes):
    owner, public, ws = homes
    before = (owner / "memories/MEMORY.md").read_text()
    items = core.scan(owner, ws)
    core.save_allowlist(owner, items, [by_title(items, "blog").id, by_title(items, "School").id])
    proj = core.sync_public(owner, public, ws)
    text = (data_dir(public) / core.PROJECTION_FILE).read_text()
    assert proj["facts"] == ["blog is lowtide.news"] and len(proj["documents"]) == 1
    assert CANARY not in text
    assert (owner / "memories/MEMORY.md").read_text() == before


def test_edited_public_section_falls_back_to_private(homes):
    owner, public, ws = homes
    items = core.scan(owner, ws)
    core.save_allowlist(owner, items, [by_title(items, "School").id])
    (ws[0] / "bio.md").write_text(f"# Bio\n## School\nCS master {CANARY}\n", encoding="utf-8")
    items = core.scan(owner, ws)
    assert core.status(by_title(items, "School"), core.load_allowlist(owner)) == "changed"
    assert CANARY not in json.dumps(core.sync_public(owner, public, ws))


def test_unpublishable_items_cannot_be_approved(homes, tmp_path):
    owner, public, ws = homes
    import sqlite3

    con = sqlite3.connect(owner / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT, source TEXT, title TEXT, started_at REAL, message_count INT)")
    con.execute("INSERT INTO sessions VALUES ('s1','cli',?,0,3)", (CANARY,))
    con.commit()
    items = core.scan(owner, ws)
    session = next(i for i in items if i.kind == "session")
    assert not session.publishable
    core.save_allowlist(owner, items, [session.id])
    assert CANARY not in json.dumps(core.sync_public(owner, public, ws))


def test_markdown_split_ignores_headings_in_code():
    parts = core.split_markdown("# A\nx\n```\n# not heading\n```\n## B\ny")
    assert [h for h, _ in parts] == ["A", "B"]


def test_plugin_renders_and_looks_up_projection(homes):
    owner, public, ws = homes
    items = core.scan(owner, ws)
    core.save_allowlist(owner, items, [by_title(items, "blog").id, by_title(items, "School").id])
    core.sync_public(owner, public, ws)
    proj = plugin.load_projection(public)
    section = plugin.render_section(proj)
    assert "blog is lowtide.news" in section and CANARY not in section and len(section) <= plugin.SECTION_MAX
    assert "CS master" in plugin.lookup(proj, "school")
    assert plugin.lookup(proj, "zzz") == "No public document matches that query."
    assert plugin.load_projection(owner) == {}


def test_file_items_carry_absolute_path(homes):
    owner, _, ws = homes
    items = core.scan(owner, ws)
    assert by_title(items, "blog").path == str((owner / "memories/MEMORY.md").resolve())
    assert by_title(items, "School").path == str((ws[0] / "bio.md").resolve())
    assert all(Path(i.path).is_absolute() for i in items if i.path)


def test_scan_discovers_new_paths_and_skips_secrets(homes):
    owner, _, ws = homes
    (owner / ".env").write_text(f"KEY={CANARY}", encoding="utf-8")
    (owner / "config.yaml").write_text("model: x", encoding="utf-8")
    (owner / "notes/2026").mkdir(parents=True)
    (owner / "notes/2026/trip.md").write_text("# Trip\nTokyo", encoding="utf-8")
    (owner / "photo.png").write_bytes(b"\x89PNG")
    (ws[0] / "new-dir").mkdir()
    (ws[0] / "new-dir/draft.txt").write_text("draft", encoding="utf-8")
    items = core.scan(owner, ws)
    paths = {i.path for i in items}
    assert str((owner / "notes/2026/trip.md").resolve()) in paths
    assert str((ws[0] / "new-dir/draft.txt").resolve()) in paths
    png = next(i for i in items if i.path.endswith("photo.png"))
    assert png.kind == "file" and not png.publishable
    assert not any(i.path.endswith((".env", "config.yaml")) for i in items)


def test_bundled_skills_folded_user_skills_listed(homes):
    owner, _, ws = homes
    for name in ("arxiv", "mine"):
        (owner / f"skills/research/{name}").mkdir(parents=True)
        (owner / f"skills/research/{name}/SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    (owner / "skills/.bundled_manifest").write_text("arxiv:abc\n", encoding="utf-8")
    titles = [i.title for i in core.scan(owner, ws) if i.kind == "skill"]
    assert "mine" in titles and "arxiv" not in titles
    assert any("Built-in skills: 1" in t for t in titles)


def test_workspace_containing_home_does_not_reexpose_excluded_files(homes):
    owner, _, _ = homes
    (owner / ".env").write_text(f"KEY={CANARY}", encoding="utf-8")
    items = core.scan(owner, [owner.parent])
    assert not any(i.path.endswith(".env") for i in items)
    assert sum(1 for i in items if i.path.endswith("MEMORY.md")) == 2


def test_kanban_db_listed_even_when_empty_then_expanded(homes):
    import sqlite3

    owner, _, ws = homes
    con = sqlite3.connect(owner / "kanban.db")
    con.executescript(
        "CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, status TEXT, created_at INT);"
        "CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT);"
    )
    con.commit()
    kb = [i for i in core.scan(owner, ws) if i.path.endswith("kanban.db")]
    assert len(kb) == 1 and "no records yet" in kb[0].title and not kb[0].publishable
    con.execute("INSERT INTO tasks VALUES ('t1', '写自传第二章', 'draft', 'todo', 1)")
    con.commit()
    con.close()
    kb = [i for i in core.scan(owner, ws) if i.path.endswith("kanban.db")]
    assert [i.title for i in kb] == ["写自传第二章 · todo"] and not kb[0].publishable


def test_rescan_sees_edits_deletions_and_wal_writes(homes):
    import sqlite3

    owner, _, ws = homes
    core.scan(owner, ws)
    (ws[0] / "bio.md").write_text("# Bio\n## School\nPhD now\n", encoding="utf-8")
    (owner / "notes.md").write_text("temp", encoding="utf-8")
    con = sqlite3.connect(owner / "state.db")
    con.executescript(
        "PRAGMA journal_mode=WAL; CREATE TABLE sessions (id TEXT, title TEXT, source TEXT, started_at REAL,"
        " message_count INT); CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT);"
    )
    core.scan(owner, ws)
    (owner / "notes.md").unlink()
    con.execute("INSERT INTO sessions VALUES ('s1', 'trip', 'cli', 1, 1)")
    con.execute("INSERT INTO messages VALUES (1, 's1', 'user', 'Tokyo plan')")
    con.commit()  # stays in state.db-wal while the connection is open
    items = core.scan(owner, ws)
    con.close()
    assert by_title(items, "School").text.endswith("PhD now")
    assert not any(i.path.endswith("notes.md") for i in items)
    assert "Tokyo plan" in by_title(items, "trip").text


def test_guest_rewrite_replaces_original_in_projection(homes):
    """An approved item can carry a guest-facing rewrite; guests get the rewrite, never the original."""
    owner, public, ws = homes
    items = core.scan(owner, ws)
    blog = by_title(items, "blog")
    core.save_allowlist(owner, items, [blog.id], {blog.id: "blog is public, ask me for the URL"})
    proj = core.sync_public(owner, public, ws)
    assert proj["facts"] == ["blog is public, ask me for the URL"]
    assert "lowtide.news" not in json.dumps(proj)
    # empty or identical rewrite -> original is published
    core.save_allowlist(owner, items, [blog.id], {blog.id: "   "})
    assert core.sync_public(owner, public, ws)["facts"] == ["blog is lowtide.news"]
    # editing the source invalidates the approval together with its rewrite
    core.save_allowlist(owner, items, [blog.id], {blog.id: "rewritten"})
    mem = owner / "memories" / "MEMORY.md"
    mem.write_text(mem.read_text(encoding="utf-8").replace("lowtide.news", "lowtide.blog"), encoding="utf-8")
    assert core.sync_public(owner, public, ws)["facts"] == []


def test_workspace_inside_home_is_listed_as_workspace(tmp_path):
    owner = tmp_path / "owner"
    ws = owner / "workspace"
    (ws / "public").mkdir(parents=True)
    (owner / "logs").mkdir()
    (ws / "public/post.md").write_text("A published post", encoding="utf-8")
    (ws / "AGENTS.md").write_text("Who I am", encoding="utf-8")
    (owner / "logs/agent.log").write_text("user said hi", encoding="utf-8")
    (owner / "config.yaml").write_text(f"terminal:\n  cwd: {ws}\n", encoding="utf-8")
    items = core.scan(owner, [ws])
    post, agents, log = by_title(items, "post.md"), by_title(items, "AGENTS.md"), by_title(items, "agent.log")
    assert post.group == "Workspace · workspace · public" and post.source == "workspace/public/post.md"
    assert agents.group == "Workspace · workspace · root" and "system prompt" in agents.note
    assert log.group == "Data dir · logs" and not log.publishable
    assert not any(i.group == "Data dir · workspace" for i in items)


def test_rewritten_document_does_not_keep_its_private_title(tmp_path):
    owner, public, ws = tmp_path / "owner", tmp_path / "public", tmp_path / "ws"
    owner.mkdir()
    ws.mkdir()
    (ws / "PrivateClientName-budget.md").write_text("# Budget\nAcme pays 40k for the Q3 audit\n", encoding="utf-8")
    items = core.scan(owner, [ws])
    doc = next(i for i in items if "PrivateClientName" in i.title)
    core.approve(owner, items, {doc.id}, {doc.id: "How Salt prices an audit\nA flat fee per report."})
    proj = core.write_projection(owner, public, [ws])
    assert [d["title"] for d in proj["documents"]] == ["How Salt prices an audit"]
    assert "PrivateClientName" not in json.dumps(proj) and "PrivateClientName" not in plugin.render_section(proj)


def test_approved_skill_becomes_a_method_the_guest_follows(tmp_path):
    from salts_secrets import guests
    owner, public = tmp_path / "owner", tmp_path / "public"
    (owner / "skills/research/bridge-checks").mkdir(parents=True)
    body = '---\nname: bridge-checks\ndescription: "Four checks on any bridge"\nauthor: Lena Moreau\n---\n# Four checks\nCount the signers.'
    (owner / "skills/research/bridge-checks/SKILL.md").write_text(body, encoding="utf-8")
    items = core.scan(owner, [])
    skill = next(i for i in items if i.kind == "skill" and i.title == "bridge-checks")
    guest = body.replace("author: Lena Moreau\n", "")
    core.approve(owner, items, {skill.id}, {skill.id: guest})
    proj = core.write_projection(owner, public, [], "Salt")
    assert proj["methods"][0]["text"] == guest and proj["documents"] == []
    assert proj["methods"][0]["title"] == "bridge-checks"
    section = plugin.render_section(proj)
    assert "How Salt works" in section and "Four checks on any bridge" in section and "Lena" not in section
    assert "Count the signers." in plugin.lookup(proj, "bridge")
    assert guests.revoked(proj, {**proj, "methods": []}) == [guest]
