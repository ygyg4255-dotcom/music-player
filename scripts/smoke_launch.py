"""Automated launch verification: runs the real application for a few
seconds on the real desktop, then closes the window and checks that the
process exits cleanly.

Usage:  .venv\\Scripts\\python.exe scripts\\smoke_launch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music_player.app import run

if __name__ == "__main__":
    code = run(sys.argv + ["--smoke"], quit_after_ms=3000)
    print(f"Launch check: exited cleanly with code {code}")
    sys.exit(code)