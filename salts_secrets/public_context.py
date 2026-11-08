"""Public profile: put the owner-approved projection in front of the agent.

The projection is written into this profile's data dir by the owner side. This module never reads
anything belonging to the owner profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from . import home

PROJECTION_FILE = "public_context.json"
SECTION_MAX = 4000
LOOKUP_MAX = 6000
TOOL_NAME = "public_notes_lookup"

LOOKUP_SCHEMA = {
    "name": TOOL_NAME,
    "description": "Search the owner's public notes (documents the owner approved for guests). Use it when a "
    "guest asks about the owner's background, writing or projects. An empty query lists titles.",
    "parameters": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Keywords or a document title"}},
        "required": [],
    },
}


def load_projection(hermes_home: Optional[Path] = None) -> dict:
    try:
        data = json.loads((home.data_dir(hermes_home) / PROJECTION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _methods(projection: dict) -> list[dict]:
    return [d for d in projection.get("methods") or [] if isinstance(d, dict)]


def _documents(projection: dict) -> list[dict]:
    return _methods(projection) + [d for d in projection.get("documents") or [] if isinstance(d, dict)]


def _summary(text: str) -> str:
    for line in str(text).splitlines():
        line = line.strip()
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip('"')[:200]
    return ""


def render_section(projection: dict) -> str:
    owner = str(projection.get("owner_name") or "the owner")
    facts = [f.strip() for f in projection.get("facts") or [] if isinstance(f, str) and f.strip()]
    lines = [f"## Notes about {owner} (everything you know; say it freely)"]
    lines += [f"- {f}" for f in facts] or [f"- (no notes yet: you know what {owner} publishes, nothing more)"]
    methods = _methods(projection)
    if methods:
        lines.append(f"\n## How {owner} works (methods {owner} published for you to use)")
        lines.append(f"When a guest asks for an analysis these fit, look the method up with {TOOL_NAME} first and "
                     "follow its steps in order. Use public data (web tools) for the numbers, keep facts, inference "
                     f"and risk apart, and say it is {owner}'s method applied by you, not a call {owner} has made.")
        lines += [f"- {m.get('title', '')}" + (f": {_summary(m.get('text', ''))}" if _summary(m.get("text", "")) else "")
                  for m in methods]
    docs = [d for d in projection.get("documents") or [] if isinstance(d, dict)]
    if docs:
        lines.append(f"\nLonger notes you can look up with {TOOL_NAME}:")
        lines += [f"- {d.get('title', '')}" for d in docs]
    text = "\n".join(lines)
    return text if len(text) <= SECTION_MAX else text[: SECTION_MAX - 20] + "\n…(truncated)"


def lookup(projection: dict, query: str = "") -> str:
    docs = _documents(projection)
    if not docs:
        return "No public documents."
    query = (query or "").strip().lower()
    if not query:
        return "Public documents:\n" + "\n".join(f"- {d.get('title', '')}" for d in docs)
    terms = query.split()
    scored = []
    for doc in docs:
        title = str(doc.get("title", ""))
        haystack = f"{title}\n{doc.get('text', '')}".lower()
        score = sum(haystack.count(t) for t in terms) + (5 if query in title.lower() else 0)
        if score:
            scored.append((score, doc))
    if not scored:
        return "No public document matches that query."
    chunks, used = [], 0
    for _, doc in sorted(scored, key=lambda pair: -pair[0]):
        chunk = f"### {doc.get('title', '')}\n{doc.get('text', '')}"[: LOOKUP_MAX - used]
        chunks.append(chunk)
        used += len(chunk)
        if used >= LOOKUP_MAX:
            break
    return "\n\n".join(chunks)


def _handle_lookup(args: Optional[dict[str, Any]] = None, **_: Any) -> str:
    return json.dumps({"result": lookup(load_projection(), (args or {}).get("query", ""))}, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_system_prompt_section(
        home.NAME, lambda _info: render_section(load_projection()), max_chars=SECTION_MAX
    )
    ctx.register_tool(
        name=TOOL_NAME,
        toolset="public_notes",
        schema=LOOKUP_SCHEMA,
        handler=_handle_lookup,
        description=LOOKUP_SCHEMA["description"],
        emoji="📖",
    )
