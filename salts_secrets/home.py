"""Where the plugin keeps its files inside a Hermes home."""

from __future__ import annotations

import os
from pathlib import Path

NAME = "salts-secrets"
PUBLIC_KEY_ENV = "SALTS_PUBLIC_API_KEY"


def current() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def data_dir(home: Path | None = None) -> Path:
    """``<home>/plugin-data/salts-secrets``: survives plugin updates and follows the profile."""
    return Path(home or current()) / "plugin-data" / NAME


def is_public(home: Path, public_profile: str) -> bool:
    home = Path(home).resolve()
    return home.parent.name == "profiles" and home.name == public_profile
