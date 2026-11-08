"""Public profile: one receipt per tool call, so the owner can see what each guest answer actually drew on.

Read from the real calls (``post_tool_call``), not from configuration: which tool ran, which public notes
``public_notes_lookup`` returned, and which projection version was live. Receipts hold public titles only.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import home, public_context

RECEIPTS_FILE = "receipts.jsonl"
ARG_KEYS = ("query", "url", "urls", "prompt")


def _titles(result: Any) -> list[str]:
    try:
        text = json.loads(result).get("result", "") if isinstance(result, str) else ""
    except ValueError:
        text = str(result or "")
    return re.findall(r"(?m)^### (.+)$", str(text))


def on_post_tool_call(tool_name: str = "", args: Optional[dict] = None, result: Any = None, session_id: str = "",
                      status: Optional[str] = None, **_: Any) -> None:
    try:
        args = args if isinstance(args, dict) else {}
        row = {"at": int(time.time()), "session_id": str(session_id or ""), "tool": tool_name,
               "status": status or "", "projection": public_context.load_projection().get("generated_at", 0),
               "args": {k: str(args[k])[:200] for k in ARG_KEYS if k in args}}
        if tool_name == public_context.TOOL_NAME:
            row["returned"] = _titles(result)
        path = home.data_dir() / RECEIPTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - an observer must never break a guest turn
        pass


def by_session(public_home: Path) -> dict[str, dict]:
    """``{session_id: {"tools": {name: count}, "looked_up": [titles], "projection": version}}``."""
    out: dict[str, dict] = {}
    try:
        lines = (home.data_dir(public_home) / RECEIPTS_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        r = out.setdefault(row.get("session_id", ""), {"tools": {}, "looked_up": [], "projection": 0})
        r["tools"][row.get("tool", "")] = r["tools"].get(row.get("tool", ""), 0) + 1
        r["looked_up"] += [t for t in row.get("returned") or [] if t not in r["looked_up"]]
        r["projection"] = max(r["projection"], int(row.get("projection") or 0))
    return out


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", on_post_tool_call)
