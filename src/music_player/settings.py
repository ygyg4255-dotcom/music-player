"""Persistent application settings with corruption recovery.

Settings live in a small JSON file under ``%LOCALAPPDATA%``.  Loading is
defensive: a missing, corrupted, or partially-written file falls back to
defaults instead of crashing the application.  Saving is atomic (write to a
temporary file, then ``os.replace``) and keeps a ``.bak`` copy of the last
known-good file for extra resilience.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import config

logger = logging.getLogger(__name__)

#: Default values used when a setting is missing, unknown, or corrupt.
DEFAULTS: dict[str, Any] = {
    "song": "",
    "duration_minutes": 30.0,
    "expanded": False,
    "start_with_windows": False,  # opt-in only; see phase 4
}

_BACKUP_SUFFIX = ".bak"


def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge *raw* over DEFAULTS, dropping unknown keys and fixing bad types."""
    result: dict[str, Any] = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in raw:
            continue
        value = raw[key]
        # Coerce to the default's type; fall back to the default when impossible.
        try:
            if isinstance(default, bool):
                result[key] = bool(value) if isinstance(value, (bool, int)) else default
            elif isinstance(default, float):
                result[key] = float(value)
            elif isinstance(default, int):
                result[key] = int(value)
            elif isinstance(default, str):
                result[key] = str(value)
            else:
                result[key] = value
        except (TypeError, ValueError):
            result[key] = default
    return result


def load(path: Path | str | None = None) -> dict[str, Any]:
    """Load settings from *path* (default: the app settings file).

    Returns a validated copy of the settings.  Never raises: missing or
    corrupt files (including a corrupt backup) yield the defaults.
    """
    path = Path(path) if path is not None else config.settings_dir() / config.SETTINGS_FILE_NAME

    for candidate in (path, Path(str(path) + _BACKUP_SUFFIX)):
        if not candidate.exists():
            continue
        try:
            with open(candidate, encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("settings root is not a JSON object")
            logger.info("Loaded settings from %s", candidate)
            return _coerce(raw)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Settings file %s unusable (%s); trying fallback", candidate, exc)
    logger.info("Using default settings (no usable file at %s)", path)
    return dict(DEFAULTS)


def save(settings: dict[str, Any], path: Path | str | None = None) -> None:
    """Atomically persist *settings* to *path*, keeping a ``.bak`` copy."""
    path = Path(path) if path is not None else config.settings_dir() / config.SETTINGS_FILE_NAME
    payload = _coerce(settings)

    path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve the previous good file as .bak before replacing it.
    if path.exists():
        try:
            os.replace(path, Path(str(path) + _BACKUP_SUFFIX))
        except OSError as exc:
            logger.warning("Could not back up settings file: %s", exc)

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, path)
        logger.debug("Saved settings to %s", path)
    except OSError as exc:
        logger.error("Failed to save settings: %s", exc)
        tmp.unlink(missing_ok=True)
        raise