
import pytest
from fastapi.testclient import TestClient

from demo.chat import server

PASS = "open sesame"


@pytest.fixture
def client(tmp_path):
    (tmp_path / "owner/memories").mkdir(parents=True)
    (tmp_path / "owner/memories/MEMORY.md").write_text("a\n§\nb", encoding="utf-8")
    s = server.Settings(
        {
            "SALT_OWNER_HOME": str(tmp_path / "owner"),
            "SALT_PUBLIC_HOME": str(tmp_path / "public"),
            "SALT_OWNER_PASSPHRASE_HASH": server.hash_passphrase(PASS),
            "SALT_SECRET": "x" * 32,
            "SALT_OWNER_KEY": "ok",
            "SALT_PUBLIC_KEY": "pk",
        }
    )
    return TestClient(server.create_app(s))


def test_passphrase_hash_roundtrip():
    h = server.hash_passphrase("p")
    assert server.verify_passphrase("p", h) and not server.verify_passphrase("q", h)
    assert h.split("$")[0] == "scrypt" and len(h.split("$")) == 3


def test_guest_by_default_and_board_locked(client):
    assert client.get("/api/me").json()["role"] == "guest"
    assert client.get("/api/board/items").status_code == 403
    assert client.post("/api/board/save", json={"public_ids": []}).status_code == 403


def test_unlock_lock_and_rate_limit(client):
    for _ in range(4):
        assert client.post("/api/unlock", json={"passphrase": "nope"}).status_code == 401
    assert client.post("/api/unlock", json={"passphrase": PASS}).json()["role"] == "owner"
    assert len(client.get("/api/board/items").json()["items"]) == 2
    assert client.post("/api/lock", json={}).json()["role"] == "guest"
    for _ in range(5):
        client.post("/api/unlock", json={"passphrase": "nope"})
    assert client.post("/api/unlock", json={"passphrase": PASS}).status_code == 429


def test_board_list_is_light_and_bodies_load_on_demand(client):
    for path in ("/api/board/text?id=x", "/api/board/search?q=a"):
        assert client.get(path).status_code == 403
    client.post("/api/unlock", json={"passphrase": PASS})
    items = client.get("/api/board/items").json()["items"]
    assert items and all("text" not in i for i in items)
    texts = {i["id"]: client.get("/api/board/text", params={"id": i["id"]}).json()["text"] for i in items}
    assert sorted(texts.values()) == ["a", "b"]
    hit = client.get("/api/board/search", params={"q": "B"}).json()["ids"]
    assert [texts[i] for i in hit] == ["b"]
    assert client.get("/api/board/text", params={"id": "gone"}).status_code == 404


def test_forged_cookie_is_guest(client):
    client.cookies.set("salt", "eyJvd25lcl9leHAiOjk5OTk5OTk5OTl9.deadbeef")
    assert client.get("/api/me").json()["role"] == "guest"


def test_json_required(client):
    assert (
        client.post(
            "/api/unlock", data="passphrase=x", headers={"content-type": "application/x-www-form-urlencoded"}
        ).status_code
        == 415
    )


def test_chat_routes_by_role(client, monkeypatch):
    seen = []

    class FakeStream:
        def __init__(self, url, headers):
            seen.append((url, headers))
            self.status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            return FakeStream(url, headers)

    monkeypatch.setattr(server.httpx, "AsyncClient", FakeClient)
    r = client.post("/api/chat", json={"message": "hello"})
    assert '"t": "hi"' in r.text
    client.post("/api/unlock", json={"passphrase": PASS})
    client.post("/api/chat", json={"message": "hello"})
    (g_url, g_h), (o_url, o_h) = seen
    assert g_url.endswith("/p/public/v1/chat/completions") and g_h["Authorization"] == "Bearer pk"
    assert o_url.endswith("/v1/chat/completions") and "/p/" not in o_url and o_h["Authorization"] == "Bearer ok"
    assert g_h["X-Hermes-Session-Id"].startswith("salt-g-") and o_h["X-Hermes-Session-Id"].startswith("salt-o-")


def test_member_codes_route_to_tier_and_blocked_cannot_chat(client, tmp_path):
    from salts_secrets import tiers as perm

    owner = tmp_path / "owner"
    m, code = perm.add_member(owner, "小王", "user")
    r = client.post("/api/unlock", json={"passphrase": code})
    assert r.json()["role"] == "user" and r.json()["name"] == "小王"
    assert client.get("/api/board/items").status_code == 403 and client.get("/api/perm").status_code == 403
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 503  # user tier key not configured
    perm.update_member(owner, m["id"], tier="blocked")
    assert client.get("/api/me").json()["role"] == "blocked"
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 403
    assert client.post("/api/lock", json={}).status_code == 403
    perm.update_member(owner, m["id"], delete=True)
    assert client.get("/api/me").json()["role"] == "blocked"  # deleting a member does not lift the block


def test_guests_page_owner_only(client):
    assert client.get("/guests", follow_redirects=False).status_code in (302, 307)
    client.post("/api/unlock", json={"passphrase": PASS})
    assert "What guests asked" in client.get("/guests").text


def test_perm_board_owner_only(client, tmp_path):
    assert client.get("/perm", follow_redirects=False).status_code in (302, 307)
    client.post("/api/unlock", json={"passphrase": PASS})
    st = client.get("/api/perm").json()
    assert st["policy"]["guest"] and any(r["locked"] for r in st["catalog"])
    assert st["planned"] == ["admin", "user"]
    for tier in ("admin", "user"):
        r = client.post("/api/perm/members", json={"action": "add", "name": "a", "tier": tier})
        assert r.status_code == 400 and "planned" in r.json()["detail"]
    j = client.post("/api/perm/members", json={"action": "add", "name": "a", "tier": "blocked"}).json()
    assert j["code"].startswith("inv-") and j["members"][0]["tier"] == "blocked" and "code_hash" not in j["members"][0]
    r = client.post("/api/perm/members", json={"action": "update", "id": j["members"][0]["id"], "tier": "user"})
    assert r.status_code == 400
    assert client.post("/api/perm/members", json={"action": "add", "name": "a", "tier": "owner"}).status_code == 400


def _owner_client(client):
    owner = TestClient(client.app)
    assert owner.post("/api/unlock", json={"passphrase": PASS}).json()["role"] == "owner"
    return owner


def _member_code(tmp_path, tier="user"):
    """A member from an older policy file: the dashboard no longer creates planned tiers, blocking still works."""
    from salts_secrets import tiers as perm

    return perm.add_member(tmp_path / "owner", "x", tier)[1]


def test_blocked_member_cannot_come_back_by_clearing_cookie(client, tmp_path):
    owner = _owner_client(client)
    client = TestClient(client.app, client=("203.0.113.7", 50000))
    code = _member_code(tmp_path)
    assert client.post("/api/unlock", json={"passphrase": code}).json()["role"] == "user"
    mid = owner.get("/api/perm").json()["members"][0]["id"]
    owner.post("/api/perm/members", json={"action": "update", "id": mid, "tier": "blocked"})
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 403
    client.cookies.clear()
    assert client.get("/api/me").json()["role"] == "blocked"
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 403
    assert client.post("/api/unlock", json={"passphrase": PASS}).json()["role"] == "owner"
    blocks = owner.get("/api/perm").json()["blocklist"]
    assert {b["kind"] for b in blocks} == {"ip", "device"}
    for b in blocks:
        owner.post("/api/perm/access", json={"action": "unblock", "kind": b["kind"], "value": b["value"]})
    assert TestClient(client.app, client=("203.0.113.7", 50001)).get("/api/me").json()["role"] == "guest"


def test_proxy_address_is_never_ip_blocked(client, tmp_path):
    owner = _owner_client(client)
    code = _member_code(tmp_path)
    proxied = TestClient(client.app, client=("172.18.0.1", 50000))
    proxied.post("/api/unlock", json={"passphrase": code})
    mid = owner.get("/api/perm").json()["members"][0]["id"]
    owner.post("/api/perm/members", json={"action": "update", "id": mid, "tier": "blocked"})
    assert [b["kind"] for b in owner.get("/api/perm").json()["blocklist"]] == ["device"]
    assert proxied.get("/api/me").json()["role"] == "blocked"
    assert TestClient(client.app, client=("172.18.0.1", 50001)).get("/api/me").json()["role"] == "guest"


def test_guest_access_switch(client):
    owner = _owner_client(client)
    owner.post("/api/perm/access", json={"action": "guest_access", "enabled": False})
    assert client.get("/api/me").json()["guest_access"] is False
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 403
    assert client.post("/api/perm/access", json={"action": "guest_access", "enabled": True}).status_code == 403


def test_only_owner_can_switch_models(client, tmp_path, monkeypatch):
    (tmp_path / "owner/config.yaml").write_text(
        "model:\n  provider: ds\n  default: deepseek-flash\nproviders:\n"
        "  bai:\n    api_key: sk-secret\n    model: claude-opus-5.5\n  ds:\n    model: deepseek-flash\n",
        encoding="utf-8",
    )
    sent = []

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            sent.append(json)
            return FakeStream()

    monkeypatch.setattr(server.httpx, "AsyncClient", FakeClient)
    assert "models" not in client.get("/api/me").json()
    assert client.post("/api/chat", json={"message": "hi", "model": "bai"}).status_code == 403
    me = client.post("/api/unlock", json={"passphrase": PASS}).json()
    assert me["models"] == [{"id": "ds", "label": "deepseek-flash"}, {"id": "bai", "label": "claude-opus-5.5"}]
    assert "sk-secret" not in str(me)
    client.post("/api/chat", json={"message": "hi"})
    client.post("/api/chat", json={"message": "hi", "model": "ds"})
    client.post("/api/chat", json={"message": "hi", "model": "bai"})
    assert client.post("/api/chat", json={"message": "hi", "model": "nope"}).status_code == 400
    plain, default, opus = sent
    assert plain == default and "provider" not in plain and plain["model"] == "hermes-agent"
    assert opus["provider"] == "bai" and opus["model"] == "claude-opus-5.5"


def test_pages_use_configured_names(tmp_path):
    s = server.Settings({"SALT_OWNER_HOME": str(tmp_path), "SALT_OWNER_NAME": "Ada", "SALT_PUBLIC_NAME": "Byte"})
    html = TestClient(server.create_app(s)).get("/").text
    assert "Byte's assistant" in html and "Hi Ada." in html
    assert "Salt" not in html and "Lena" not in html


def test_board_classify_and_save_with_decisions(client):
    client.post("/api/unlock", json={"passphrase": PASS})
    res = client.post("/api/board/classify", json={}).json()
    assert set(res["counts"]) == {"A", "B", "C"} and res["model"] is False
    items = client.get("/api/board/items").json()["items"]
    assert all("classification" in i for i in items)
    ids = [i["id"] for i in items if i["publishable"]]
    out = client.post("/api/board/save", json={"public_ids": ids, "decisions": {i: {"via": "bulk-A"} for i in ids}}).json()
    assert len(out["rejected"]) == len(ids) and out["facts"] == 0  # nothing is A without a model
    out = client.post("/api/board/save", json={"public_ids": ids[:1], "decisions": {ids[0]: {"via": "single"}}}).json()
    assert out["facts"] == 1 and out["rejected"] == []


VIEW = "look only"


def _viewer_app(tmp_path):
    (tmp_path / "owner/memories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "owner/memories/MEMORY.md").write_text("a\n§\nb", encoding="utf-8")
    s = server.Settings({
        "SALT_OWNER_HOME": str(tmp_path / "owner"), "SALT_PUBLIC_HOME": str(tmp_path / "public"),
        "SALT_OWNER_PASSPHRASE_HASH": server.hash_passphrase(PASS),
        "SALT_VIEWER_PASSPHRASE_HASH": server.hash_passphrase(VIEW), "SALT_VIEWER_PASSPHRASE_SHOWN": VIEW,
        "SALT_SECRET": "x" * 32, "SALT_OWNER_KEY": "ok", "SALT_PUBLIC_KEY": "pk"})
    return server.create_app(s)


def test_readonly_passphrase_reads_but_never_writes(tmp_path):
    c = TestClient(_viewer_app(tmp_path))
    me = c.post("/api/unlock", json={"passphrase": VIEW}).json()
    assert me["role"] == "owner" and me["readonly"] and "models" not in me
    assert c.get("/api/board/items").status_code == 200
    assert c.get("/api/perm").status_code == 200 and c.get("/perm").status_code == 200
    for path, body in [("/api/board/save", {"public_ids": []}), ("/api/board/classify", {"force": True}),
                       ("/api/board/policy", {"text": "x"}), ("/api/board/draft", {"id": "x"}),
                       ("/api/board/guests/clear", {}), ("/api/perm/save", {"tiers": {}}),
                       ("/api/perm/access", {"action": "guest_access", "enabled": False})]:
        r = c.post(path, json=body)
        assert r.status_code == 403 and r.json()["detail"] == "read-only mode", path
    owner = TestClient(c.app)
    assert "readonly" not in owner.post("/api/unlock", json={"passphrase": PASS}).json()
    assert c.post("/api/lock", json={}).json()["role"] == "guest"
    assert c.get("/api/board/items").status_code == 403


def test_readonly_chat_uses_tool_free_sessions(tmp_path, monkeypatch):
    seen = []

    class FakeStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            seen.append((url, headers["X-Hermes-Session-Id"], json))
            return FakeStream()

    monkeypatch.setattr(server.httpx, "AsyncClient", FakeClient)
    c = TestClient(_viewer_app(tmp_path))
    c.post("/api/unlock", json={"passphrase": VIEW})
    assert c.post("/api/chat", json={"message": "x" * (server.VIEWER_CHARS + 1)}).status_code == 400
    for _ in range(server.VIEWER_TURNS + 1):
        c.post("/api/chat", json={"message": "hi", "model": "other"})
    sids = [sid for _, sid, _ in seen]
    assert all(sid.startswith(server.VIEWER_PREFIX) for sid in sids)
    assert len(set(sids[:server.VIEWER_TURNS])) == 1 and sids[-1] != sids[0]
    assert all("/p/" not in url and set(body) == {"model", "stream", "messages"} for url, _, body in seen)
    c.post("/api/new", json={})
    c.post("/api/chat", json={"message": "hi"})
    assert seen[-1][1].startswith(server.VIEWER_PREFIX) and seen[-1][1] != sids[-1]


def test_doc_describes_this_install(tmp_path):
    c = TestClient(_viewer_app(tmp_path))
    assert c.get("/doc").status_code == 200
    d = c.get("/api/doc").json()
    assert d["viewer"] and d["viewer_hint"] == VIEW and d["facts"] == 0 and d["documents"] == []
    assert "web_search" in d["tools"] and "terminal" not in d["tools"]


def test_hourly_turn_limit_spares_the_owner(tmp_path, monkeypatch):
    class Done:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

        def stream(self, *a, **k):
            return self

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda *a, **k: Done())
    s = server.Settings({"SALT_OWNER_HOME": str(tmp_path / "owner"), "SALT_PUBLIC_HOME": str(tmp_path / "public"),
                         "SALT_OWNER_PASSPHRASE_HASH": server.hash_passphrase(PASS), "SALT_SECRET": "x" * 32,
                         "SALT_OWNER_KEY": "ok", "SALT_PUBLIC_KEY": "pk", "SALT_TURNS_PER_HOUR": "2"})
    app = server.create_app(s)
    guest = TestClient(app, client=("203.0.113.9", 1))
    assert [guest.post("/api/chat", json={"message": "q"}).status_code for _ in range(3)] == [200, 200, 429]
    assert TestClient(app, client=("203.0.113.9", 2)).post("/api/chat", json={"message": "q"}).status_code == 429
    assert TestClient(app, client=("198.51.100.4", 1)).post("/api/chat", json={"message": "q"}).status_code == 200
    owner = TestClient(app, client=("203.0.113.9", 3))
    owner.post("/api/unlock", json={"passphrase": PASS})
    assert owner.post("/api/chat", json={"message": "q"}).status_code == 200


def test_turn_limit_counts_devices_behind_a_proxy_and_skips_refused_requests(tmp_path, monkeypatch):
    class Done:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            yield "data: [DONE]"

        def stream(self, *a, **k):
            return self

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda *a, **k: Done())
    env = {"SALT_OWNER_HOME": str(tmp_path / "owner"), "SALT_PUBLIC_HOME": str(tmp_path / "public"),
           "SALT_SECRET": "x" * 32, "SALT_OWNER_KEY": "ok", "SALT_PUBLIC_KEY": "pk", "SALT_TURNS_PER_HOUR": "1"}
    app = server.create_app(server.Settings(env))
    first, second = TestClient(app, client=("172.18.0.1", 1)), TestClient(app, client=("172.18.0.1", 2))
    assert first.post("/api/chat", json={"message": "q"}).status_code == 200
    assert first.post("/api/chat", json={"message": "q"}).status_code == 429
    assert second.post("/api/chat", json={"message": "q"}).status_code == 200
    keyless = TestClient(server.create_app(server.Settings(dict(env, SALT_PUBLIC_KEY=""))), client=("203.0.113.5", 1))
    assert [keyless.post("/api/chat", json={"message": "q"}).status_code for _ in range(2)] == [503, 503]


def test_negative_turn_limit_is_refused_at_startup(tmp_path):
    with pytest.raises(ValueError, match="SALT_TURNS_PER_HOUR"):
        server.Settings({"SALT_OWNER_HOME": str(tmp_path / "owner"), "SALT_PUBLIC_HOME": str(tmp_path / "public"),
                         "SALT_SECRET": "x" * 32, "SALT_TURNS_PER_HOUR": "-1"})


def test_unanswered_message_gives_the_turn_back(tmp_path, monkeypatch):
    class Down:
        status_code = 502

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aread(self):
            return b"bad gateway"

        def stream(self, *a, **k):
            return self

    monkeypatch.setattr(server.httpx, "AsyncClient", lambda *a, **k: Down())
    app = server.create_app(server.Settings({"SALT_OWNER_HOME": str(tmp_path / "owner"),
                                             "SALT_PUBLIC_HOME": str(tmp_path / "public"), "SALT_SECRET": "x" * 32,
                                             "SALT_PUBLIC_KEY": "pk", "SALT_TURNS_PER_HOUR": "1"}))
    guest = TestClient(app, client=("203.0.113.7", 1))
    for _ in range(3):
        r = guest.post("/api/chat", json={"message": "q"})
        assert r.status_code == 200 and "Hermes 502" in r.text
