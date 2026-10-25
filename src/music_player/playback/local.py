"""Local file playback through the Windows default application.

``os.startfile`` (ShellExecute "open") launches the file with whatever
application the user has associated with its type.  That choice is the
user's; this module never changes file associations.

Capability honesty: ShellExecute returns no handle to the launched player,
so this source **cannot** pause playback or read its state by itself.
``Capabilities(can_pause=False, can_read_state=False)`` says so explicitly.
System-wide pause is still possible through the Windows media-session API
(:mod:`music_player.media_control`): the source exposes a ``control_hint``
(the default player's exe name, e.g. "potplayermini64") so the media layer
can find that session — verified to work with PotPlayer on this machine.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from pathlib import Path

from .base import Capabilities, PlaybackError, PlaybackSource, PlaybackState

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=32)
def default_player_hint(extension: str) -> str | None:
    """Exe name of the default app for *extension*, for GSMTC matching.

    Resolution order mirrors Windows itself:

    1. the per-user ``UserChoice`` association (Windows 8+; this is what
       ``os.startfile`` actually uses — HKCR alone can lie),
    2. the legacy HKCR association chain (.ext -> progid -> open command).

    Returns the executable's base name in lowercase, or None when the
    association is a UWP/AppX app (no stable exe name to match against)
    or cannot be resolved.  None is safe: the media layer then falls back
    to picking the unique playing session.
    """
    extension = extension.lower()
    if not extension.startswith("."):
        return None
    try:
        import winreg

        progid = None
        try:  # 1. per-user UserChoice wins
            user_choice = (
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                f"\\FileExts\\{extension}\\UserChoice"
            )
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, user_choice) as key:
                progid, _ = winreg.QueryValueEx(key, "ProgId")
        except OSError:
            pass
        if not progid:  # 2. legacy HKCR association
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, extension) as key:
                progid, _ = winreg.QueryValueEx(key, None)
        if not progid or progid.startswith("AppX"):
            return None  # UWP app: no exe name to hint with

        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, f"{progid}\\shell\\open\\command"
        ) as key:
            command, _ = winreg.QueryValueEx(key, None)
    except OSError:
        return None
    match = re.search(r'"([^"]+?\.exe)"', command) or re.search(
        r"([A-Za-z]:\\[^\s]+?\.exe)", command, re.IGNORECASE
    )
    if match is None:
        return None
    return Path(match.group(1)).stem.lower()


class LocalFileSource(PlaybackSource):
    """Open an audio file with the user's default Windows application."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            Capabilities(label="Local file (default app)", can_pause=False, can_read_state=False)
        )
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def control_hint(self) -> str | None:
        """Default player's exe name, e.g. "potplayermini64" (see module doc)."""
        return default_player_hint(self._path.suffix)

    @property
    def control_title(self) -> str | None:
        """The file stem, to recognize this song's media session by title.

        Players report the *metadata* title (e.g. "Karma Police" for a file
        named "Radiohead - Karma Police.mp3"); the media layer's fuzzy
        token-set matching bridges that difference — verified live against
        the machine's default player.
        """
        return self._path.stem

    @property
    def waits_for_playback(self) -> bool:
        # os.startfile returns no handle and the default player is arbitrary:
        # the countdown must not consume the duration until the player is
        # actually playing, confirmed via the media session.
        return True

    @property
    def waiting_message(self) -> str:
        return f"Opening “{self._path.stem}” — the countdown starts once it is playing."

    def play(self) -> None:
        """Launch the file.  Raises PlaybackError if it cannot be opened."""
        if not self._path.exists():
            raise PlaybackError(f"File not found: {self._path}")
        if not self._path.is_file():
            raise PlaybackError(f"Not a file: {self._path}")
        try:
            # Non-blocking: ShellExecute returns immediately, the player
            # runs in its own process and the GUI thread stays responsive.
            os.startfile(str(self._path))  # noqa: S606 - intentional, Windows-only API
        except OSError as exc:
            raise PlaybackError(f"Could not open {self._path.name}: {exc}") from exc
        logger.info("Opened local file with default app: %s", self._path)

    def pause(self) -> bool:
        # Honest: no handle to the default player.  See module docstring.
        return False

    def state(self) -> PlaybackState:
        return PlaybackState.UNKNOWN