"""Owner profile: a session opened with the demo's read-only passphrase talks to the owner agent but runs no tool."""

from __future__ import annotations

from typing import Any, Optional

VIEWER_PREFIX = "salt-v-"


def on_pre_tool_call(tool_name: str = "", session_id: str = "", **_: Any) -> Optional[dict[str, str]]:
    if str(session_id or "").startswith(VIEWER_PREFIX):
        return {"action": "block", "message": f"Tool '{tool_name}' is off in read-only mode."}
    return None


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
