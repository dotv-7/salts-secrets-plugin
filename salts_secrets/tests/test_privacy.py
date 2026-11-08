import pytest

from salts_secrets import privacy
from salts_secrets.home import data_dir

RULES = privacy.load_rules()
GROUPS = {"credentials", "identity", "assets", "health_family", "source", "dates"}


def _cases(kind):
    for rule in RULES.rules:
        for example in rule.examples.get(kind) or []:
            yield pytest.param(rule.name, example, id=f"{rule.name}:{str(example)[:30]}")


def _item(example):
    return example if isinstance(example, dict) else {"kind": "workspace", "title": "", "text": example}


def test_every_rule_has_a_hit_and_a_miss_example():
    assert {r.group for r in RULES.rules} >= GROUPS
    for rule in RULES.rules:
        assert rule.examples.get("hit") and rule.examples.get("miss"), rule.name
        assert rule.description, rule.name


@pytest.mark.parametrize("name,example", list(_cases("hit")))
def test_rule_hits_its_examples(name, example):
    assert name in privacy.screen(_item(example), RULES)["rules_hit"]


@pytest.mark.parametrize("name,example", list(_cases("miss")))
def test_rule_misses_its_counter_examples(name, example):
    assert name not in privacy.screen(_item(example), RULES)["rules_hit"]


def test_clean_published_text_has_no_hits():
    text = ("Three lines first on any stablecoin attestation: the date of the report, the signature of "
            "the accounting firm, and the liabilities line. If any is missing, the reserves are a story.")
    res = privacy.screen({"kind": "workspace", "title": "Low Tide #1", "text": text}, RULES)
    assert res == {"rules_hit": [], "floor": "A", "quasi": []}


def test_floor_is_worst_tier_hit():
    res = privacy.screen({"kind": "memory", "text": "Wi-Fi password lisboa2024, birthday 3 April"}, RULES)
    assert res["floor"] == "C" and {"credential_words", "wifi_router", "birthday_age", "specific_date"} <= set(res["rules_hit"])


def test_owner_dictionary_is_appended_and_matched_as_words(tmp_path):
    local = data_dir(tmp_path) / privacy.LOCAL_RULES_FILE
    local.parent.mkdir(parents=True)
    local.write_text(
        "dictionary:\n  real_names: [Lena Moreau, Moreau]\n  family: [Tomás, Clara]\n  places: [Bonfim]\n"
        "rules:\n  - name: project_codename\n    group: custom\n    tier: C\n    patterns: ['\\bnightjar\\b']\n",
        encoding="utf-8",
    )
    rules = privacy.load_rules(tmp_path)
    assert privacy.screen({"text": "picked up by tomás"}, rules)["rules_hit"] == ["dictionary_family"]
    assert "dictionary_real_name" in privacy.screen({"text": "Lena Moreau"}, rules)["rules_hit"]
    assert privacy.screen({"text": "Clarabelle and Moreauville"}, rules)["rules_hit"] == []
    assert privacy.screen({"text": "Nightjar ships"}, rules)["floor"] == "C"
    assert privacy.screen({"text": "Bonfim"}, rules) == {"rules_hit": ["dictionary_place"], "floor": "B",
                                                          "quasi": ["place"]}
    assert "real_names" in privacy.load_rules().dictionary and not privacy.load_rules().dictionary["real_names"]


def test_combination_marks_every_quasi_item():
    results = {
        "a": privacy.screen({"text": "She is based in a coastal city"}, RULES),
        "b": privacy.screen({"text": "Her day job is product design"}, RULES),
        "c": privacy.screen({"text": "Three lines on attestations"}, RULES),
    }
    combo = privacy.combination(results, RULES)
    assert combo["hit"] and sorted(combo["ids"]) == ["a", "b"] and combo["kinds"] == ["place", "profession"]
    assert "combination" in results["a"]["rules_hit"] and results["c"]["rules_hit"] == []
    single = {"a": privacy.screen({"text": "based in Lisbon"}, RULES)}
    assert not privacy.combination(single, RULES)["hit"]


def test_rules_need_tier_b_or_c():
    with pytest.raises(ValueError):
        privacy.load_rules(extra={"rules": [{"name": "x", "tier": "A", "patterns": ["x"]}]})


def test_mode_600_from_the_real_file(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("plain")
    f.chmod(0o600)
    assert "owner_only_file" in privacy.screen({"path": str(f), "text": "plain"}, RULES)["rules_hit"]
    f.chmod(0o644)
    assert "owner_only_file" not in privacy.screen({"path": str(f), "text": "plain"}, RULES)["rules_hit"]
