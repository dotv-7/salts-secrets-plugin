"""PyYAML when installed, else ruamel.yaml (a Hermes dependency): the official Hermes image ships no PyYAML."""

from __future__ import annotations

import io
from typing import Any

try:
    from yaml import YAMLError, safe_dump, safe_load
except ImportError:
    from ruamel.yaml import YAML
    from ruamel.yaml.error import YAMLError

    def safe_load(text: str) -> Any:
        return YAML(typ="safe", pure=True).load(text)

    def safe_dump(data: Any, sort_keys: bool = False, allow_unicode: bool = True) -> str:
        yaml = YAML(typ="safe", pure=True)
        yaml.default_flow_style = False
        yaml.allow_unicode = allow_unicode
        out = io.StringIO()
        yaml.dump(data, out)
        return out.getvalue()


__all__ = ["YAMLError", "safe_dump", "safe_load"]
