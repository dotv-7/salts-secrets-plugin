"""Public profile: refuse every tool call outside the guest allowlist.

The public profile already exposes only safe toolsets; this hook is the second line of defence against
misconfiguration, plugin/MCP tools and background forks. Allowed tools only get http(s)
locations: any argument that names a local path is refused, whatever the tool. The allowlist comes from ``tier.json`` (written
by ``hermes salts-secrets sync``) and is always capped by ``CEILING``: nothing that can read the owner's
machine or act as the owner is ever allowed. Any internal error blocks the call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from . import home

DEFAULT_ALLOWED_TOOLS = frozenset(
    {
        "web_search",
        "web_extract",
        "clarify",
        "vision_analyze",
        "image_generate",
        "text_to_speech",
        "public_notes_lookup",
    }
)
CEILING = DEFAULT_ALLOWED_TOOLS | frozenset(
    {
        "x_search",
        "video_analyze",
        "video_generate",
        "xai_video_edit",
        "xai_video_extend",
    }
)
TIER_FILE = "tier.json"


def allowed_tools(hermes_home: Optional[Path] = None) -> frozenset[str]:
    path = home.data_dir(hermes_home) / TIER_FILE
    if not path.exists():
        return DEFAULT_ALLOWED_TOOLS
    data = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(t for t in data.get("allowed_tools") or [] if isinstance(t, str)) & CEILING


def _block(message: str) -> dict[str, str]:
    return {"action": "block", "message": message}


def _is_remote_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


# Argument names that point at a source or destination. On a local terminal backend Hermes resolves a
# non-URL value against the owner's filesystem (tools/image_source.py), so a guest may only pass http(s).
_LOCATION_KEY = re.compile(r"(?:^|_)(?:urls?|paths?|files?|sources?|images?|audio|video|dir|directory)$", re.IGNORECASE)
_LOCAL_PATH = re.compile(r"^(?:/|~|\.{1,2}[\\/]|\\\\|file:|[a-z]:[\\/])", re.IGNORECASE)
_MAX_DEPTH = 6


def looks_local(value: str) -> bool:
    r"""True for anything Hermes could open on the owner machine: /abs, ~, ./, ../, file:, \\share, C:\."""
    return bool(_LOCAL_PATH.match(value.strip()))


def _bad_argument(args: Any, key: str = "", depth: int = 0) -> Optional[str]:
    """Name of the first argument that is a local path, or a non-http(s) value under a location key."""
    if depth > _MAX_DEPTH:
        return key or "arguments"
    if isinstance(args, dict):
        for k, v in args.items():
            bad = _bad_argument(v, str(k), depth + 1)
            if bad:
                return bad
        return None
    if isinstance(args, (list, tuple)):
        for v in args:
            bad = _bad_argument(v, key, depth + 1)
            if bad:
                return bad
        return None
    if isinstance(args, str):
        if looks_local(args):
            return key or "argument"
        if args.strip() and _LOCATION_KEY.search(key) and not _is_remote_url(args):
            return key
    return None


def check(tool_name: str, args: dict, hermes_home: Optional[Path] = None) -> Optional[dict[str, str]]:
    if tool_name not in allowed_tools(hermes_home):
        return _block(f"Tool '{tool_name}' is not available in a guest session.")
    if tool_name == "vision_analyze" and not _is_remote_url(args.get("image_url")):
        return _block("In a guest session vision_analyze only accepts http(s) image URLs.")
    bad = _bad_argument(args)
    if bad:
        return _block(f"In a guest session '{bad}' must be an http(s) URL; local paths are refused.")
    return None


def on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict[str, str]]:
    try:
        return check(str(tool_name or ""), args if isinstance(args, dict) else {})
    except Exception:  # noqa: BLE001
        return _block("Guest tool gate error; tool call refused.")


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
