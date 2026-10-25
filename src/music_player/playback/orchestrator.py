"""Playback orchestration: local-first workflow without blocking the GUI.

On ``start(song)`` the orchestrator runs the Music-folder search on a
thread-pool thread and, back on the GUI thread:

* match found      → open it with the default Windows application
* no match          → YouTube fallback (injected in phase 4; runs on the
                      pool thread because it performs network I/O;
                      absent here the status line says so honestly)
* search error      → understandable error message, no crash

Stale results are dropped: every search carries a sequence number, so a
restart while a previous search is still running cannot apply the old
result to the new request.
"""

from __future__ import annotations

import logging

from PySide6 import QtCore

from .. import local_search
from .base import PlaybackError, PlaybackSource
from .local import LocalFileSource

logger = logging.getLogger(__name__)


class _SignalRelay(QtCore.QObject):
    """Lives on the GUI thread; the pool thread emits through it."""

    done = QtCore.Signal(int, object, str)  # (sequence, MatchResult|None, error)
    fallback_failed = QtCore.Signal(int, str)  # (sequence, error message)


class _SearchTask(QtCore.QRunnable):
    """Runs the local search off the GUI thread."""

    def __init__(self, sequence: int, song: str, relay: _SignalRelay) -> None:
        super().__init__()
        self._sequence = sequence
        self._song = song
        self._relay = relay

    def run(self) -> None:  # executes on a thread-pool thread
        try:
            match = local_search.search(self._song)
            self._relay.done.emit(self._sequence, match, "")
        except Exception as exc:  # defensive: never crash the pool thread
            logger.exception("Local search failed")
            self._relay.done.emit(self._sequence, None, str(exc))


class _FallbackTask(QtCore.QRunnable):
    """Runs the YouTube fallback off the GUI thread.

    The fallback fetches the YouTube search page to determine the first
    result before opening it — blocking network I/O that must never touch
    the GUI thread.  Signal emissions through the relay are queued and so
    are safe from this thread.
    """

    def __init__(
        self,
        orchestrator: "PlaybackOrchestrator",
        sequence: int,
        song: str,
        fallback,
        relay: _SignalRelay,
    ) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._sequence = sequence
        self._song = song
        self._fallback = fallback
        self._relay = relay

    def run(self) -> None:  # executes on a thread-pool thread
        if self._orchestrator._sequence != self._sequence:
            return  # request was superseded while this task waited to run
        try:
            self._fallback(self._song)
        except Exception as exc:  # defensive: never crash the pool thread
            logger.exception("YouTube fallback failed")
            self._relay.fallback_failed.emit(self._sequence, str(exc))


class PlaybackOrchestrator(QtCore.QObject):
    """Coordinates search → playback → fallback for one song request."""

    #: status line update: (message, is_error)
    status = QtCore.Signal(str, bool)
    #: a playback source was successfully started (None = nothing started)
    source_started = QtCore.Signal(object)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._sequence = 0
        self._source: PlaybackSource | None = None
        self._current_song = ""
        self._youtube_fallback: object | None = None  # callable(song) set in phase 4
        self._relay = _SignalRelay(self)
        self._relay.done.connect(self._on_search_done)
        self._relay.fallback_failed.connect(self._on_fallback_failed)
        # Keep the in-flight QRunnable alive: without a Python reference the
        # shiboken wrapper can be garbage-collected before the pool runs it.
        self._task: _SearchTask | None = None
        self._fallback_task: _FallbackTask | None = None

    # --- wiring -----------------------------------------------------------

    def set_youtube_fallback(self, fallback) -> None:
        """Register the phase-4 YouTube fallback: ``fallback(song)``."""
        self._youtube_fallback = fallback

    def report_source(self, source: PlaybackSource) -> None:
        """Let the fallback (YouTube, phase 4) register its own source."""
        self._source = source
        self.source_started.emit(source)

    @property
    def current_source(self) -> PlaybackSource | None:
        return self._source

    # --- workflow ------------------------------------------------------------

    def start(self, song: str) -> None:
        """Search for *song* locally (background) and open the best match."""
        song = song.strip()
        if not song:
            self.status.emit("Enter a song name first.", True)
            return
        # A new request invalidates the previous workflow's source: a stale
        # source must never be paused or controlled on behalf of this song.
        self._source = None
        self._current_song = song
        self._sequence += 1
        self._task = _SearchTask(self._sequence, song, self._relay)
        QtCore.QThreadPool.globalInstance().start(self._task)
        self.status.emit(f"Searching your Music folder for “{song}”…", False)

    def _on_search_done(self, sequence: int, match, error: str) -> None:
        if sequence != self._sequence:
            return  # stale result from an abandoned request
        self._task = None
        if error:
            self.status.emit(f"Local search failed: {error}", True)
            return
        if match is None:
            if self._youtube_fallback is not None:
                # Off the GUI thread: the fallback performs network I/O to
                # determine the first result before opening it.  Status and
                # source_started reach the GUI via queued relay signals.
                self._fallback_task = _FallbackTask(
                    self, self._sequence, self._current_song,
                    self._youtube_fallback, self._relay,
                )
                QtCore.QThreadPool.globalInstance().start(self._fallback_task)
            else:
                self.status.emit(
                    f"No local match for “{self._current_song}” — YouTube fallback lands in phase 4.",
                    False,
                )
            return

        source = LocalFileSource(match.path)
        try:
            source.play()
        except PlaybackError as exc:
            self.status.emit(str(exc), True)
            return
        self._source = source
        self.source_started.emit(source)
        score = f" (match {match.score:.0f}%)" if match.method == "fuzzy" else ""
        self.status.emit(
            f"Playing “{match.path.stem}” from your Music folder{score}", False
        )

    def _on_fallback_failed(self, sequence: int, message: str) -> None:
        if sequence != self._sequence:
            return  # stale result from an abandoned request
        self._fallback_task = None
        self.status.emit(f"YouTube fallback failed: {message}", True)

    # --- control -------------------------------------------------------------

    def pause_playback(self) -> tuple[PlaybackSource | None, bool]:
        """Try to pause what this app started.

        Returns ``(source, paused)`` where ``source`` is None when nothing
        was started by this app, and ``paused`` is False when the source
        cannot be paused (the caller may then try system-level control).
        """
        if self._source is None:
            return None, False
        return self._source, self._source.pause()

    def reset(self) -> None:
        """Forget the current source (e.g. when the countdown is cancelled)."""
        self._source = None