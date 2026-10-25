#!/usr/bin/env python3
"""Entry point for the Dynamic Island Music Player.

Optional CLI flags (used by automated verification and the packaged exe):

* ``--smoke`` — close automatically after a few seconds (launch check).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the src/ package importable without installation (also used by
# the PyInstaller build, which freezes this file as the entry point).
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from music_player.app import run  # noqa: E402

if __name__ == "__main__":
    if "--smoke" in sys.argv:
        sys.exit(run(quit_after_ms=4000))
    sys.exit(run())