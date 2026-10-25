"""Optional "start with Windows" registration (per-user HKCU Run key).

Design rules from the spec:

* the option is *disabled by default* — nothing is registered unless the
  user explicitly ticks the checkbox in the app;
* enabling writes exactly one value (``APP_ID``) under the current user's
  Run key — no services, no scheduled tasks, no machine-wide changes;
* disabling deletes **only** the value this app created;
* every function returns an error message on failure (None on success)
  instead of raising, so the UI can report problems honestly.
"""

from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command() -> str:
    """Command line that starts this application after Windows login."""
    if getattr(sys, "frozen", False):  # PyInstaller exe
        return f'"{sys.executable}"'
    # Source run: pythonw with the project's main.py (parent of src/).
    main_py = Path(__file__).resolve().parents[2] / "main.py"
    return f'"{sys.executable}" "{main_py}"'


def is_enabled(key_path: str = RUN_KEY) -> bool:
    """Whether this app's startup registration exists."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.QueryValueEx(key, config.APP_ID)
            return True
    except OSError:
        return False


def enable(key_path: str = RUN_KEY) -> str | None:
    """Register this app to start with Windows.  None = success."""
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, config.APP_ID, 0, winreg.REG_SZ, _launch_command())
    except OSError as exc:
        logger.error("Startup enable failed: %s", exc)
        return f"Could not register the startup entry: {exc}"
    logger.info("Startup registration written (value %r)", config.APP_ID)
    return None


def disable(key_path: str = RUN_KEY) -> str | None:
    """Remove this app's startup registration (only its own value).  None = success."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, config.APP_ID)
    except FileNotFoundError:
        return None  # already gone — that is the requested state
    except OSError as exc:
        logger.error("Startup disable failed: %s", exc)
        return f"Could not remove the startup entry: {exc}"
    logger.info("Startup registration removed (value %r)", config.APP_ID)
    return None