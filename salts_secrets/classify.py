"""A/B/C classification of context items: rules first (``privacy.py``), then the owner's model.

  A  publish as-is: no personal data, already published or pure method
  B  needs a guest rewrite: personal data around a shareable core
  C  never public: credentials, accounts, addresses, contacts, health, family names, unpublished calls

An item is A only when no rule hits and the model says A. Without a model nothing is A. Results are
cached per item fingerprint in the owner's plugin data, so reopening the page is instant. Classifying
never publishes anything: every item still needs an owner click (single or bulk) on the page.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

from . import _yaml as yaml
from . import privacy
from .home import data_dir

CACHE_FILE = "classifications.json"
BATCH = 20
TEXT_MAX = 900
PARALLEL = 4
UNCLASSIFIED = "not checked by the model yet: rules only, so it is not A"

Ask = Callable[[str, str], Awaitable[str]]

SYSTEM = """You sort pieces of a private assistant's context into three tiers before the owner decides what
strangers may see. The owner is a pseudonymous writer; strangers talk to her public assistant.

A = publish as-is. No personal data at all: text she already published (articles, posts, newsletter
    issues) or pure method (rules, checklists, thresholds she states publicly).
B = needs a rewrite. Personal data around a core that can be shared in general terms: where she lives,
    her job, pets, routines, schedules, preferences, opinions about named private people.
C = never public. Credentials, accounts, wallet or bank details, addresses, phone or email, health,
    names of family, partners, friends or doctors, threats, legal trouble, unpublished judgments, anything
    she marked private.

When unsure, pick the stricter tier. Reply with JSON only:
{"items": [{"id": "<id>", "tier": "A|B|C", "reason": "<one short sentence, no personal details>"}]}"""


def _cache_path(owner_home: Path) -> Path:
    return data_dir(owner_home) / CACHE_FILE


def load_cache(owner_home: Path) -> dict:
    try:
        data = json.loads(_cache_path(owner_home).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(owner_home: Path, cache: dict) -> None:
    from .context import _atomic_write

    _atomic_write(_cache_path(owner_home), json.dumps(cache, ensure_ascii=False, indent=1))


def host_llm():
    """The owner's model through Hermes (``agent.plugin_llm``, what ``ctx.llm`` hands out): Hermes resolves the
    provider, credentials (config, ``.env`` or OAuth) and fallback; the plugin never sees a key. The dashboard
    API has no plugin context, so it binds the same facade to this plugin's id, without any override."""
    from agent.plugin_llm import PluginLlm

    from .home import NAME

    return PluginLlm(plugin_id=NAME)


def host_asker(llm: Any = None, purpose: str = "classify", max_tokens: int = 2500, temperature: float = 0) -> Ask:
    llm = llm or host_llm()

    async def ask(system: str, user: str) -> str:
        result = await llm.acomplete(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens, purpose=f"salts-secrets.{purpose}",
        )
        return result.text or ""

    return ask


def parse_reply(text: str) -> dict[str, dict]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    out = {}
    for row in data.get("items") or [] if isinstance(data, dict) else []:
        if isinstance(row, dict) and row.get("tier") in privacy.TIERS and row.get("id"):
            out[str(row["id"])] = {"tier": row["tier"], "reason": str(row.get("reason") or "")[:200]}
    return out


def _prompt(items: list) -> str:
    rows = [{"id": it.id, "source": it.group, "title": it.title, "text": it.text[:TEXT_MAX]} for it in items]
    return "Classify each item:\n" + json.dumps(rows, ensure_ascii=False)


def combine(screen: dict, model: Optional[dict], rules: privacy.Ruleset) -> dict:
    """Final tier = the stricter of the rule floor and the model; no model answer means at least B."""
    names = rules.by_name()
    model_tier = (model or {}).get("tier")
    tier = privacy.worst(screen["floor"], model_tier or "B")
    if screen["rules_hit"] and privacy.TIERS.index(screen["floor"]) >= privacy.TIERS.index(model_tier or "A"):
        first = names.get(screen["rules_hit"][0])
        reason = (first.description if first else "") or f"rule {screen['rules_hit'][0]}"
    else:
        reason = (model or {}).get("reason") or UNCLASSIFIED
    return {"tier": tier, "reason": reason, "rules_hit": screen["rules_hit"], "rule_floor": screen["floor"],
            "model_tier": model_tier or "", "quasi": screen.get("quasi", [])}


def cached(owner_home: Path, items: Iterable, rules: Optional[privacy.Ruleset] = None) -> dict[str, dict]:
    """Classification of every publishable item from rules + cached model answers (no model call)."""
    rules = rules or privacy.load_rules(owner_home)
    cache = load_cache(owner_home)
    out = {}
    for it in items:
        if not it.publishable:
            continue
        hit = cache.get(it.id)
        model = hit if hit and hit.get("fingerprint") == it.fingerprint else None
        out[it.id] = dict(combine(privacy.screen(it, rules), model, rules), classified=bool(model))
    return out


async def classify(owner_home: Path, items: list, ask: Optional[Ask], force: bool = False) -> dict:
    """Ask the model about every publishable item without a cached answer, then return ``cached``."""
    rules = privacy.load_rules(owner_home)
    cache = load_cache(owner_home)
    todo = [it for it in items if it.publishable and (force or (cache.get(it.id) or {}).get("fingerprint") != it.fingerprint)]
    errors, answered = [], []
    if ask and todo:
        sem = asyncio.Semaphore(PARALLEL)

        async def one(batch: list) -> None:
            async with sem:
                try:
                    answers = parse_reply(await ask(SYSTEM, _prompt(batch)))
                except Exception as exc:  # noqa: BLE001
                    errors.append(type(exc).__name__)
                    return
            answered.append(len(answers))
            for it in batch:
                if it.id in answers:
                    cache[it.id] = {**answers[it.id], "fingerprint": it.fingerprint, "at": int(time.time())}

        await asyncio.gather(*(one(todo[i : i + BATCH]) for i in range(0, len(todo), BATCH)))
        _save_cache(owner_home, cache)
    result = cached(owner_home, items, rules)
    counts = {t: sum(1 for r in result.values() if r["tier"] == t) for t in privacy.TIERS}
    model = bool(ask) and (not todo or bool(answered))  # a model that failed every batch left rules only
    return {"items": result, "counts": counts, "model": model, "errors": errors,
            "rules": privacy.describe(rules)}


def run(owner_home: Path, items: list, ask: Optional[Ask] = None, force: bool = False) -> dict:
    """Synchronous wrapper for the dashboard API (FastAPI runs sync handlers in a worker thread)."""
    return asyncio.run(classify(owner_home, items, ask, force))


DRAFT_SYSTEM = """You write the public version of one private note for a pseudonymous writer's public assistant;
strangers will read it. Keep what is safe in general terms (her published work, methods, the kind of
place or job), remove names, places below country level, dates, accounts, amounts, health and anything
marked private. Do not add facts. Same language as the source, one short paragraph, no preamble.
If nothing can be shared, reply with exactly: NOTHING_SAFE_TO_SHARE"""
NOTHING = "NOTHING_SAFE_TO_SHARE"


async def draft(item, ask: Ask) -> str:
    """A guest version of one B item from the owner's model; "" when nothing in it is safe to share."""
    text = (await ask(DRAFT_SYSTEM, f"Source: {item.group} › {item.title}\n\n{item.text[:8000]}")).strip()
    return "" if not text or NOTHING in text else text
