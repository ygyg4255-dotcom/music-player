"""Local-first song discovery in the user's Music known folder.

The Music directory is resolved through the Windows known-folder API
(``SHGetKnownFolderPath`` with ``FOLDERID_Music``) so OneDrive-redirected or
relocated Music folders work — the path is never assumed.

Matching strategy:

1. Exact: the normalized query equals a normalized file title.
2. Fuzzy: RapidFuzz ``token_set_ratio`` against the file title; the best
   score wins.  Scores below ``config.MATCH_MIN_THRESHOLD`` are rejected so
   unrelated files are never chosen — the caller falls back to YouTube.

The module never modifies, moves, or deletes anything in the Music folder;
it only reads directory entries.
"""

from __future__ import annotations

import ctypes
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, utils as rf_utils

from . import config

logger = logging.getLogger(__name__)

#: Windows known-folder id for the per-user Music directory.
FOLDERID_MUSIC = uuid.UUID("{4BD8D571-6D19-48D3-BE97-422220080E43}")

#: Upper bound on files inspected per scan, so a pathological folder can
#: never wedge the search thread.
MAX_FILES_PER_SCAN = 20_000


@dataclass(frozen=True)
class MatchResult:
    """A chosen local file."""

    path: Path
    #: 0-100 RapidFuzz-style score; 100 for exact matches.
    score: float
    #: How the match was found: "exact" or "fuzzy".
    method: str


def music_dir() -> Path | None:
    """Return the user's Music known folder, or None if unresolvable.

    Uses the Win32 known-folder API (no hard-coded path), falling back to
    ``%USERPROFILE%\\Music`` if the API is unavailable for any reason.
    """
    path = _known_folder_music()
    if path is not None and path.is_dir():
        return path
    fallback = Path.home() / "Music"
    return fallback if fallback.is_dir() else None


def _known_folder_music() -> Path | None:
    """Resolve FOLDERID_Music via SHGetKnownFolderPath (None on failure)."""
    try:
        buffer = ctypes.c_void_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.create_string_buffer(FOLDERID_MUSIC.bytes_le, 16),
            0,  # KF_FLAG_DEFAULT
            None,
            ctypes.byref(buffer),
        ) == 0:  # S_OK
            path_str = ctypes.wstring_at(buffer.value)
            ctypes.windll.ole32.CoTaskMemFree(buffer)
            return Path(path_str)
    except Exception:  # pragma: no cover - defensive, platform-dependent
        logger.exception("SHGetKnownFolderPath failed")
    return None


def _title_of(path: Path) -> str:
    """Filename without extension (the "song title" for matching)."""
    return path.stem


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, and collapse whitespace for comparison."""
    return " ".join(rf_utils.default_process(text).split())


def scan_audio(folder: Path) -> list[Path]:
    """Recursively collect supported audio files under *folder*.

    Unreadable subfolders are skipped; the scan is bounded by
    ``MAX_FILES_PER_SCAN``.  The Music folder itself is the ceiling — no
    other directories are ever touched.
    """
    files: list[Path] = []
    if not folder.is_dir():
        return files
    try:
        for current, _dirs, names in os.walk(folder, onerror=lambda e: None):
            for name in names:
                if Path(name).suffix.lower() in config.SUPPORTED_EXTENSIONS:
                    files.append(Path(current) / name)
                    if len(files) >= MAX_FILES_PER_SCAN:
                        logger.warning("Music scan hit the %d-file cap", MAX_FILES_PER_SCAN)
                        return files
    except OSError as exc:
        logger.warning("Music scan stopped on error: %s", exc)
    return files


def find_song(query: str, files: list[Path]) -> MatchResult | None:
    """Pick the best local file for *query*, or None when nothing fits.

    Exact normalized-title equality wins outright.  Otherwise the best
    RapidFuzz ``token_set_ratio`` score must reach
    ``config.MATCH_MIN_THRESHOLD``; anything weaker returns None so the
    caller can fall back to YouTube.
    """
    query = query.strip()
    if not query or not files:
        return None

    normalized_query = _normalize(query)

    # 1. Exact filename match (normalized).
    for path in files:
        if _normalize(_title_of(path)) == normalized_query:
            return MatchResult(path=path, score=100.0, method="exact")

    # 2. Fuzzy match against titles.
    best: MatchResult | None = None
    for path in files:
        title = _normalize(_title_of(path))
        if not title:
            continue
        score = fuzz.token_set_ratio(normalized_query, title)
        if best is None or score > best.score:
            best = MatchResult(path=path, score=float(score), method="fuzzy")

    if best is not None and best.score >= config.MATCH_MIN_THRESHOLD:
        return best

    logger.info(
        "No local match for %r (best fuzzy score %.0f below %d)",
        query,
        best.score if best else -1,
        config.MATCH_MIN_THRESHOLD,
    )
    return None


def search(query: str, folder: Path | None = None) -> MatchResult | None:
    """Convenience: resolve the Music folder (if needed), scan, and match."""
    if folder is None:
        folder = music_dir()
        if folder is None:
            logger.info("No resolvable Music folder; skipping local search")
            return None
    return find_song(query, scan_audio(folder))