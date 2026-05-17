"""Manage default export and project settings."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULTS_DIR = Path(".dabble-tasks")
DEFAULTS_FILE = DEFAULTS_DIR / "defaults.json"


def ensure_defaults_dir() -> None:
    """Create the defaults directory if it doesn't exist."""
    DEFAULTS_DIR.mkdir(parents=True, exist_ok=True)


def load_defaults() -> dict[str, Any]:
    """Load default settings from the defaults file."""
    if DEFAULTS_FILE.exists():
        return json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    return {}


def save_defaults(defaults: dict[str, Any]) -> None:
    """Save default settings to the defaults file."""
    ensure_defaults_dir()
    DEFAULTS_FILE.write_text(json.dumps(defaults, ensure_ascii=False, indent=2), encoding="utf-8")


def set_default(key: str, value: str | None) -> dict[str, Any]:
    """Set a default value. If value is None, unset the key."""
    defaults = load_defaults()
    if value is None:
        defaults.pop(key, None)
    else:
        defaults[key] = value
    save_defaults(defaults)
    return defaults


def get_default(key: str) -> str | None:
    """Get a default value."""
    defaults = load_defaults()
    return defaults.get(key)


def get_export_default() -> str | None:
    """Get the default export path."""
    return get_default("export")


def get_project_default() -> str | None:
    """Get the default project."""
    return get_default("project")


def set_export_default(path: str | None) -> dict[str, Any]:
    """Set the default export path."""
    return set_default("export", path)


def set_project_default(project: str | None) -> dict[str, Any]:
    """Set the default project."""
    return set_default("project", project)


def get_model_default() -> str | None:
    """Get the default summary model."""
    return get_default("model")


def set_model_default(model: str | None) -> dict[str, Any]:
    """Set the default summary model."""
    return set_default("model", model)


def get_base_url_default() -> str | None:
    """Get the default OpenAI-compatible base URL."""
    return get_default("base_url")


def set_base_url_default(base_url: str | None) -> dict[str, Any]:
    """Set the default OpenAI-compatible base URL."""
    return set_default("base_url", base_url)
