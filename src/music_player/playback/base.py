"""The playback-source interface and capability reporting.

Phase 3 of the spec requires a "clear playback-management interface that
reports the capabilities and state of the playback source when available".
Every concrete source declares a :class:`Capabilities` object; the
orchestrator and UI consult it instead of assuming control is possible.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass


class PlaybackError(Exception):
    """Raised when a playback operation fails (missing file, launch error…)."""


class PlaybackState(enum.Enum):
    """Coarse playback state, where the source can report one."""

    UNKNOWN = "unknown"    # source cannot read state
    PLAYING = "playing"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(frozen=True)
class Capabilities:
    """What a playback source can actually do — reported, never assumed."""

    #: Short human description, e.g. "Local file (default app)".
    label: str
    #: Whether pause() can actually pause the started playback.
    can_pause: bool = False
    #: Whether state() can report more than UNKNOWN.
    can_read_state: bool = False


class PlaybackSource(ABC):
    """Something that starts music playback (local file, browser tab, …)."""

    def __init__(self, capabilities: Capabilities) -> None:
        self._capabilities = capabilities

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    @property
    def control_hint(self) -> str | None:
        """Substring identifying this source's app in Windows media sessions.

        Used by :mod:`music_player.media_control` to find the right GSMTC
        session (e.g. "potplayermini64", "brave").  None = no known hint.
        """
        return None

    @property
    def control_title(self) -> str | None:
        """Title of the media this source starts (e.g. the requested song).

        Used by :mod:`music_player.media_control` to recognize the Windows
        media session belonging to this playback when several sessions
        exist (e.g. several Brave tabs).  None = no known title; session
        picking then falls back to app hints only.
        """
        return None

    @property
    def waits_for_playback(self) -> bool:
        """True when the countdown must wait until playback is confirmed.

        Sources that hand off to an external app which may or may not
        actually start playing (e.g. a browser tab that can be blocked by
        the browser's autoplay policy) use this so the user's timer
        duration is not consumed before the song is really playing.
        """
        return False

    @abstractmethod
    def play(self) -> None:
        """Start playback.  Raises :class:`PlaybackError` on failure."""

    def pause(self) -> bool:
        """Attempt to pause.  Returns False when unsupported or failed.

        The default implementation honestly reports "cannot pause".
        """
        return False

    def state(self) -> PlaybackState:
        """Current playback state; UNKNOWN when the source cannot tell."""
        return PlaybackState.UNKNOWN