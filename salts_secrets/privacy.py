"""Privacy screening rules: the first layer of the A/B/C classification on the Context page.

Rules live in ``privacy_rules.yaml`` next to this file, plus the owner's own additions in
``<HERMES_HOME>/plugin-data/salts-secrets/privacy_rules.yaml``. A rule hit sets the lowest tier an item can
get (B or C); the model may raise it, never lower it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import _yaml as yaml
from .home import data_dir

RULES_FILE = Path(__file__).resolve().parent / "privacy_rules.yaml"
LOCAL_RULES_FILE = "privacy_rules.yaml"
TIERS = ("A", "B", "C")


def worst(*tiers: Optional[str]) -> str:
    return max((t for t in tiers if t in TIERS), key=TIERS.index, default="A")


@dataclass
class Rule:
    name: str
    group: str
    tier: str
    description: str = ""
    patterns: list = field(default_factory=list)
    source: dict = field(default_factory=dict)
    quasi: str = ""
    examples: dict = field(default_factory=dict)
    hard: bool = False


@dataclass
class Ruleset:
    rules: list
    dictionary: dict
    dictionary_rules: dict
    combination: dict

    def by_name(self) -> dict:
        out = {r.name: r for r in self.rules}
        for spec in self.dictionary_rules.values():
            out.setdefault(spec["rule"], Rule(spec["rule"], spec.get("group", "identity"), spec.get("tier", "C"),
                                              quasi=spec.get("quasi", "")))
        out.setdefault(self.combination.get("rule", "combination"),
                       Rule(self.combination.get("rule", "combination"), "combination", "B",
                            self.combination.get("message", "")))
        return out


def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


def _rule(raw: dict) -> Rule:
    if raw.get("tier") not in ("B", "C"):
        raise ValueError(f"rule {raw.get('name')!r}: tier must be B or C")
    return Rule(
        name=str(raw["name"]),
        group=str(raw.get("group") or "custom"),
        tier=raw["tier"],
        description=str(raw.get("description") or ""),
        patterns=[_compile(p) for p in raw.get("patterns") or []],
        source=dict(raw.get("source") or {}),
        quasi=str(raw.get("quasi") or ""),
        examples=dict(raw.get("examples") or {}),
        hard=bool(raw.get("hard")),
    )


def _read(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def load_rules(owner_home: Optional[Path] = None, extra: Optional[dict] = None) -> Ruleset:
    """Bundled rules, then the owner's file (its dictionary lists are appended, its rules added)."""
    base = _read(RULES_FILE)
    layers = [base]
    if owner_home is not None:
        layers.append(_read(data_dir(owner_home) / LOCAL_RULES_FILE))
    if extra:
        layers.append(extra)
    dictionary: dict[str, list[str]] = {}
    dictionary_rules = dict(base.get("dictionary_rules") or {})
    rules: list[Rule] = []
    for layer in layers:
        for key, words in (layer.get("dictionary") or {}).items():
            dictionary.setdefault(str(key), []).extend(str(w).strip() for w in words or [] if str(w).strip())
        for key, spec in (layer.get("dictionary_rules") or {}).items():
            dictionary_rules[str(key)] = spec
        rules += [_rule(r) for r in layer.get("rules") or []]
    for key in dictionary:
        dictionary_rules.setdefault(key, {"rule": f"dictionary_{key}", "group": "identity", "tier": "C"})
    return Ruleset(rules, dictionary, dictionary_rules, dict(base.get("combination") or {}))


def _words_pattern(words: Iterable[str]) -> Optional[re.Pattern]:
    parts = sorted({w for w in words if w}, key=len, reverse=True)
    if not parts:
        return None
    alt = "|".join(re.escape(w) for w in parts)
    return re.compile(rf"(?<![A-Za-z0-9_])(?:{alt})(?![A-Za-z0-9_])", re.IGNORECASE)


def _get(item: Any, key: str, default: Any = "") -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _mode(item: Any) -> str:
    mode = _get(item, "mode", "") or (_get(item, "meta", {}) or {}).get("mode", "")
    if mode:
        return str(mode)
    path = _get(item, "path", "")
    try:
        return oct(os.stat(path).st_mode & 0o777)[2:] if path and Path(path).is_file() else ""
    except OSError:
        return ""


def _source_hit(rule: Rule, item: Any) -> bool:
    src = rule.source
    if not src:
        return False
    path = str(_get(item, "path", "") or _get(item, "source", "")).replace("\\", "/")
    if "kinds" in src and _get(item, "kind", "") not in src["kinds"]:
        return False
    if "path" in src and not _compile(src["path"]).search(path):
        return False
    if "mode" in src and _mode(item) != str(src["mode"]):
        return False
    if "text" in src and not _compile(src["text"]).search(str(_get(item, "text", ""))):
        return False
    return True


def screen_text(text: str, ruleset: Ruleset) -> list[Rule]:
    by_name = ruleset.by_name()
    hits = [r for r in ruleset.rules if r.patterns and any(p.search(text) for p in r.patterns)]
    for key, words in ruleset.dictionary.items():
        pattern = _words_pattern(words)
        spec = ruleset.dictionary_rules.get(key) or {}
        if pattern and pattern.search(text) and spec.get("rule") in by_name:
            hits.append(by_name[spec["rule"]])
    return hits


def hard_stops(text: str, ruleset: Ruleset) -> list[str]:
    """Rules marked ``hard`` that the text hits: no click, single or bulk, publishes such a text."""
    return [r.name for r in ruleset.rules if r.hard and any(p.search(text) for p in r.patterns)]


def screen(item: Any, ruleset: Ruleset) -> dict:
    """``{"rules_hit": [...], "floor": "A"|"B"|"C", "quasi": [...]}`` for one item (text, title and origin)."""
    text = f"{_get(item, 'title', '')}\n{_get(item, 'text', '')}"
    hits = screen_text(text, ruleset) + [r for r in ruleset.rules if _source_hit(r, item)]
    names = list(dict.fromkeys(r.name for r in hits))
    return {
        "rules_hit": names,
        "floor": worst(*(r.tier for r in hits)),
        "quasi": sorted({r.quasi for r in hits if r.quasi}),
    }


def combination(results: dict[str, dict], ruleset: Ruleset) -> dict:
    """Cross-item rule: if the items published together carry ``needs`` or more kinds of quasi-identifier
    (city, profession, age...), every item that carries one is marked B. Mutates ``results``."""
    spec = ruleset.combination or {}
    kinds = set(spec.get("kinds") or [])
    seen = {k for r in results.values() for k in r.get("quasi", []) if k in kinds}
    if len(seen) < int(spec.get("needs") or 2):
        return {"hit": False, "ids": [], "kinds": sorted(seen)}
    name = spec.get("rule", "combination")
    ids = []
    for item_id, r in results.items():
        if set(r.get("quasi", [])) & kinds:
            r["rules_hit"] = list(dict.fromkeys([*r["rules_hit"], name]))
            r["floor"] = worst(r["floor"], spec.get("tier", "B"))
            ids.append(item_id)
    return {"hit": True, "ids": ids, "kinds": sorted(seen), "message": spec.get("message", "")}


def describe(ruleset: Ruleset) -> dict:
    """Rule names to their description and tier, for the page's tooltips."""
    return {n: {"group": r.group, "tier": r.tier, "description": r.description} for n, r in ruleset.by_name().items()}
