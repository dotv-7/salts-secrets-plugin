import asyncio
import json
import time

import pytest

from salts_secrets import classify
from salts_secrets import context as core
from salts_secrets.home import data_dir


@pytest.fixture
def owner(tmp_path):
    home = tmp_path / "owner"
    (home / "memories").mkdir(parents=True)
    entries = []
    for i in range(34):
        entries.append(f"Low Tide method {i}: check the attestation signature before the reserves line.")
    for i in range(33):
        entries.append(f"Salt has a cat and moved to a coastal city, note {i}.")
    for i in range(33):
        entries.append(f"Wi-Fi password for flat {i} is lisboa{i}")
    (home / "memories/MEMORY.md").write_text("\n§\n".join(entries), encoding="utf-8")
    return home


def fake_model(answer_for):
    calls = []

    async def ask(system, user):
        calls.append(user)
        rows = json.loads(user.split("\n", 1)[1])
        return json.dumps({"items": [{"id": r["id"], "tier": answer_for(r["text"]), "reason": "fake"} for r in rows]})

    return ask, calls


def tiers(result):
    return {t: sorted(i for i, r in result["items"].items() if r["tier"] == t) for t in "ABC"}


def test_model_cannot_lift_rule_hits_and_nothing_is_a_without_model(owner):
    items = core.scan(owner, [])
    no_model = classify.run(owner, items, None)
    assert no_model["counts"]["A"] == 0 and not no_model["model"]
    ask, calls = fake_model(lambda text: "A")  # a model that says everything is safe
    result = classify.run(owner, items, ask)
    t = tiers(result)
    assert len(t["A"]) == 34 and len(t["C"]) == 33 and len(t["B"]) == 33
    wifi = next(r for r in result["items"].values() if r["tier"] == "C")
    assert {"credential_words", "wifi_router"} <= set(wifi["rules_hit"]) and wifi["model_tier"] == "A"
    assert len(calls) == 5  # 100 items in batches of 20
    classify.run(owner, items, ask)
    assert len(calls) == 5  # cached by fingerprint


def test_model_can_raise_a_clean_item(owner):
    items = core.scan(owner, [])
    ask, _ = fake_model(lambda text: "C" if "method 3:" in text else "A")
    result = classify.run(owner, items, ask)
    method3 = next(i for i in items if "method 3:" in i.text)
    assert result["items"][method3.id]["tier"] == "C"


def test_bulk_never_publishes_c_and_every_fact_traces_to_a_click(owner):
    items = core.scan(owner, [])
    ask, _ = fake_model(lambda text: "A")
    started = time.time()
    result = classify.run(owner, items, ask)
    t = tiers(result)
    everything = [i.id for i in items]
    decisions = {i: {"via": "bulk-A"} for i in everything}  # a forged "approve all as A" request
    rewrites = {i: "Salt has a cat." for i in t["B"]}
    decisions.update({i: {"via": "bulk-B"} for i in t["B"]})
    decisions.update({i: {"via": "bulk-B"} for i in t["C"]})  # bulk-B must not take C either
    approved = core.approve(owner, items, everything, rewrites, decisions)
    assert {r["id"] for r in approved["rejected"]} == set(t["C"])
    proj = core.sync_public(owner, owner / "profiles/public", [])
    assert time.time() - started < 300
    assert not any("password" in f for f in proj["facts"]) and len(proj["facts"]) == 67
    log = {row["id"]: row for row in core.read_approval_log(owner) if row["decision"] == "public"}
    for meta in proj["fact_meta"]:
        row = log[meta["id"]]
        assert row["decided_by"] == "owner" and row["decided_via"] in ("bulk-A", "bulk-B")
        assert meta["tier"] == row["classifier_tier"] and meta["mode"] == ("rewrite" if meta["tier"] == "B" else "as-is")
    allow = core.load_allowlist(owner)
    assert all(e["decided_via"] and "rules_hit" in e and e["classifier_tier"] for e in allow.values())


def test_single_click_overrides_are_logged(owner):
    items = core.scan(owner, [])
    classify.run(owner, items, fake_model(lambda text: "A")[0])
    wifi = next(i for i in items if "Wi-Fi" in i.text)
    core.approve(owner, items, [wifi.id], {}, {wifi.id: {"via": "single"}})
    row = [r for r in core.read_approval_log(owner) if r["id"] == wifi.id][-1]
    assert row["override"] is True and row["classifier_tier"] == "C" and row["decided_via"] == "single"
    assert core.load_allowlist(owner)[wifi.id]["override"] is True
    core.approve(owner, items, [], {}, {wifi.id: {"via": "single"}})
    assert core.read_approval_log(owner)[-1]["decision"] == "private"


def test_bulk_a_does_not_take_b_items(owner):
    items = core.scan(owner, [])
    t = tiers(classify.run(owner, items, fake_model(lambda text: "A")[0]))
    approved = core.approve(owner, items, t["B"], {}, {i: {"via": "bulk-A"} for i in t["B"]})
    assert len(approved["rejected"]) == 33 and approved["entries"] == {}


def test_combination_flags_published_batch(tmp_path):
    home = tmp_path / "owner"
    (home / "memories").mkdir(parents=True)
    (home / "memories/MEMORY.md").write_text("Salt is based in a coastal city\n§\nHer day job is design", encoding="utf-8")
    items = core.scan(home, [])
    approved = core.approve(home, items, [i.id for i in items])
    assert approved["combination"]["hit"] and len(approved["combination"]["ids"]) == 2
    assert all(e.get("combination") for e in approved["entries"].values())


def test_parse_reply_tolerates_fences_and_junk():
    assert classify.parse_reply('```json\n{"items":[{"id":"x","tier":"B","reason":"r"}]}\n```') == {
        "x": {"tier": "B", "reason": "r"}}
    assert classify.parse_reply("no json") == {} and classify.parse_reply('{"items":[{"id":"x","tier":"Z"}]}') == {}


def test_failed_model_call_leaves_items_unclassified(owner):
    async def boom(system, user):
        raise OSError("down")

    result = asyncio.run(classify.classify(owner, core.scan(owner, []), boom))
    assert result["errors"] and result["counts"]["A"] == 0
    assert not (data_dir(owner) / classify.CACHE_FILE).read_text().strip("{} \n")
