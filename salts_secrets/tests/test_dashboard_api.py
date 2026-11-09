import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from salts_secrets.home import data_dir

CANARY = "CANARY-PRIVATE-91c2"
API = Path(__file__).resolve().parents[1] / "dashboard/plugin_api.py"


@pytest.fixture
def client(tmp_path, monkeypatch):
    owner = tmp_path / "owner"
    public = owner / "profiles/strangers"
    (owner / "memories").mkdir(parents=True)
    public.mkdir(parents=True)
    (owner / "memories/MEMORY.md").write_text(f"writes a newsletter\n§\nwifi password {CANARY}", encoding="utf-8")
    (owner / "config.yaml").write_text(
        "plugins:\n  entries:\n    salts-secrets:\n      settings:\n"
        "        public_profile: strangers\n        owner_name: Ada\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("salts_secrets.home.current", lambda: owner)
    spec = importlib.util.spec_from_file_location("salts_secrets_dashboard_api", API)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.home, "current", lambda: owner)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/salts-secrets")
    return TestClient(app), owner, public


def test_items_are_private_and_paths_redacted(client):
    http, owner, _ = client
    body = http.get("/api/plugins/salts-secrets/items").json()
    assert body["owner_name"] == "Ada"
    assert body["items"] and all(i["status"] == "private" for i in body["items"])
    assert str(owner) not in json.dumps(body)
    assert {i["path"] for i in body["items"]} == {"<HERMES_HOME>/memories/MEMORY.md"}


def test_publish_projects_only_ticked_items_into_named_public_profile(client):
    http, _, public = client
    items = http.get("/api/plugins/salts-secrets/items").json()["items"]
    ok = next(i for i in items if i["title"].startswith("writes a newsletter"))
    res = http.post("/api/plugins/salts-secrets/publish", json={"public_ids": [ok["id"]]}).json()
    assert res["facts"] == 1
    projection = (data_dir(public) / "public_context.json").read_text(encoding="utf-8")
    assert "writes a newsletter" in projection and CANARY not in projection and '"owner_name": "Ada"' in projection
    assert http.get("/api/plugins/salts-secrets/preview").json()["facts"] == ["writes a newsletter"]


def test_permissions_never_offer_locked_toolsets(client):
    http, owner, _ = client
    catalog = http.get("/api/plugins/salts-secrets/permissions").json()["catalog"]
    locked = [t["id"] for t in catalog if t["locked"]]
    assert "terminal" in locked
    http.post("/api/plugins/salts-secrets/permissions", json={"guest": ["web", "terminal"]})
    assert http.get("/api/plugins/salts-secrets/permissions").json()["guest"] == ["web"]


def test_check_reports_broken_without_setup(client):
    http, _, _ = client
    body = http.get("/api/plugins/salts-secrets/check").json()
    assert body["ok"] is False and "BROKEN" in body["line"]


def test_classify_and_bulk_publish_never_take_c(client):
    http, owner, public = client
    items = http.get("/api/plugins/salts-secrets/items").json()["items"]
    res = http.post("/api/plugins/salts-secrets/classify", json={}).json()
    assert res["model"] is False and res["counts"]["A"] == 0  # no model configured: rules only
    wifi = next(i for i in items if "wifi" in i["title"])
    assert res["items"][wifi["id"]]["tier"] == "C" and "credential_words" in res["items"][wifi["id"]]["rules_hit"]
    ids = [i["id"] for i in items]
    out = http.post("/api/plugins/salts-secrets/publish", json={
        "public_ids": ids, "public_texts": {i: "rewritten" for i in ids},
        "decisions": {i: {"via": "bulk-B"} for i in ids}}).json()
    assert [r["id"] for r in out["rejected"]] == [wifi["id"]]
    projection = (data_dir(public) / "public_context.json").read_text(encoding="utf-8")
    assert CANARY not in projection
    meta = json.loads(projection)["fact_meta"]
    assert meta and all(m["decided_via"] == "bulk-B" and m["mode"] == "rewrite" for m in meta)
    listed = http.get("/api/plugins/salts-secrets/items").json()["items"]
    assert all(i["classification"] for i in listed if i["publishable"])


def test_a_failed_model_call_is_a_502(client, monkeypatch):
    http, _, _ = client

    async def no_model(system, prompt):
        raise RuntimeError("no provider configured")

    monkeypatch.setattr("_salts_secrets_dashboard.classify.host_asker", lambda *a, **k: no_model)
    item = http.get("/api/plugins/salts-secrets/items").json()["items"][0]
    res = http.post("/api/plugins/salts-secrets/draft", json={"id": item["id"]})
    assert res.status_code == 502 and "RuntimeError" in res.text


def test_opening_the_tab_sends_nothing_to_the_model(client, monkeypatch):
    http, _, _ = client
    sent = []

    async def ask(system, prompt):
        sent.append(prompt)
        return "{}"

    monkeypatch.setattr("_salts_secrets_dashboard.classify.host_asker", lambda *a, **k: ask)
    opened = http.post("/api/plugins/salts-secrets/classify", json={"rules_only": True}).json()
    assert sent == [] and opened["model"] is False and opened["items"]
    assert not any("no model configured" in it["reason"] for it in opened["items"].values())
    http.post("/api/plugins/salts-secrets/classify", json={})
    assert sent
