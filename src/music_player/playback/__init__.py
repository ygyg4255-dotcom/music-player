"""Playback sources and orchestration.

A *source* is something that can start music playback and — depending on
the mechanism — may or may not be able to pause it or read its state.  The
interface is deliberately explicit about what each source can do, so the UI
never claims control that the underlying player does not actually expose.
"""

from __future__ import annotations

from .base import Capabilities, PlaybackError, PlaybackSource, PlaybackState
from .local import LocalFileSource
from .orchestrator import PlaybackOrchestrator

__all__ = [
    "Capabilities",
    "PlaybackError",
    "PlaybackSource",
    "PlaybackState",
    "LocalFileSource",
    "PlaybackOrchestrator",
]