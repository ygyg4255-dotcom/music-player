"""Central configuration: constants, thresholds, and file locations."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Player Music"
#: Stable storage identifier (settings folder, log folder, HKCU Run value
#: name).  Deliberately NOT derived from the display name: renaming it
#: would orphan existing user settings and any registered startup entry.
APP_ID = "dynamic-island-music-player"
VERSION = "0.1.0"

# --- Settings storage ----------------------------------------------------


def settings_dir() -> Path:
    """Return the per-user settings directory under %LOCALAPPDATA%."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID


SETTINGS_FILE_NAME = "settings.json"

# --- Local music search --------------------------------------------------

#: Audio extensions considered during the local Music-folder scan.
SUPPORTED_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".wma",
    ".opus",
}

# --- Song matching (RapidFuzz scores, 0-100) -------------------------------

#: Score at/above which a match is treated as an exact title hit.
MATCH_EXACT_THRESHOLD = 95
#: Score at/above which a candidate is a strong close match.
MATCH_CLOSE_THRESHOLD = 85
#: Minimum acceptable score; anything lower falls back to YouTube.
MATCH_MIN_THRESHOLD = 62

# --- Countdown timer ------------------------------------------------------

#: Shortest duration (minutes) accepted from user input.
MIN_DURATION_MINUTES = 0.1
#: Longest duration (minutes) accepted from user input (7 days).
MAX_DURATION_MINUTES = 10_080

# --- Browser / YouTube fallback --------------------------------------------

YOUTUBE_SEARCH_URL = "https://www.youtube.com/results?search_query={query}"
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"