"""Countdown timer component.

A small, UI-independent, testable countdown built on Python's monotonic
clock.  The remaining time is always *computed* as ``end - now``; nothing
ever subtracts one second per tick, so drift cannot accumulate and pause/
resume is exact.

States:

    IDLE -> RUNNING -> EXPIRED
                ^        |
                |        v
              PAUSED    (reset -> IDLE)

``check()`` evaluates the clock and is called both by an internal ticker
(100 ms) and by tests with a monkeypatched monotonic clock, making the
whole state machine deterministically testable without an event loop.
"""

from __future__ import annotations

import enum
import logging
import math
from time import monotonic

from PySide6 import QtCore

logger = logging.getLogger(__name__)


class TimerState(enum.Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    EXPIRED = "expired"


class CountdownTimer(QtCore.QObject):
    """Monotonic-clock countdown with start/pause/resume/cancel/expire."""

    #: emitted when the timer starts; payload is the duration in seconds
    started = QtCore.Signal(float)
    #: emitted on every evaluation while running; payload is remaining seconds
    tick = QtCore.Signal(float)
    #: emitted when paused; payload is the remaining seconds at pause time
    paused = QtCore.Signal(float)
    #: emitted when resumed; payload is the remaining seconds at resume time
    resumed = QtCore.Signal(float)
    cancelled = QtCore.Signal()
    expired = QtCore.Signal()

    TICK_INTERVAL_MS = 100

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._state = TimerState.IDLE
        self._duration_seconds = 0.0
        self._end_monotonic = 0.0
        self._remaining_at_pause = 0.0

        self._ticker = QtCore.QTimer(self)
        self._ticker.setInterval(self.TICK_INTERVAL_MS)
        self._ticker.timeout.connect(self.check)

    # --- queries -----------------------------------------------------------

    @property
    def state(self) -> TimerState:
        return self._state

    @property
    def duration_seconds(self) -> float:
        return self._duration_seconds

    def remaining_seconds(self) -> float:
        """Remaining seconds right now (0.0 when idle or expired)."""
        if self._state is TimerState.PAUSED:
            return self._remaining_at_pause
        if self._state is not TimerState.RUNNING:
            return 0.0
        return max(0.0, self._end_monotonic - monotonic())

    # --- transitions --------------------------------------------------------

    def start(self, duration_seconds: float) -> None:
        """Start counting down *duration_seconds* from now.

        Raises ValueError for a non-positive duration.
        """
        duration_seconds = float(duration_seconds)
        if not math.isfinite(duration_seconds) or duration_seconds <= 0:
            raise ValueError("duration must be a positive finite number")

        self._duration_seconds = duration_seconds
        self._end_monotonic = monotonic() + duration_seconds
        self._state = TimerState.RUNNING
        self._ticker.start()
        logger.debug("Timer started: %.1fs", duration_seconds)
        self.started.emit(duration_seconds)
        self.tick.emit(duration_seconds)

    def pause(self) -> bool:
        """Pause the running countdown.  Returns False if not running."""
        if self._state is not TimerState.RUNNING:
            return False
        self._remaining_at_pause = self.remaining_seconds()
        self._state = TimerState.PAUSED
        self._ticker.stop()
        logger.debug("Timer paused with %.1fs left", self._remaining_at_pause)
        self.paused.emit(self._remaining_at_pause)
        return True

    def resume(self) -> bool:
        """Resume from pause, continuing with the stored remaining time.

        Returns False if not paused.
        """
        if self._state is not TimerState.PAUSED:
            return False
        self._end_monotonic = monotonic() + self._remaining_at_pause
        self._state = TimerState.RUNNING
        self._ticker.start()
        logger.debug("Timer resumed with %.1fs left", self._remaining_at_pause)
        self.resumed.emit(self._remaining_at_pause)
        self.tick.emit(self.remaining_seconds())
        return True

    def cancel(self) -> None:
        """Abort the countdown and return to idle.  Safe from any state."""
        was_active = self._state in (TimerState.RUNNING, TimerState.PAUSED)
        self._ticker.stop()
        self._state = TimerState.IDLE
        self._duration_seconds = 0.0
        self._remaining_at_pause = 0.0
        if was_active:
            logger.debug("Timer cancelled")
            self.cancelled.emit()

    def reset(self) -> None:
        """Return an expired (or any) timer to idle, without cancelled()."""
        self._ticker.stop()
        self._state = TimerState.IDLE
        self._duration_seconds = 0.0
        self._remaining_at_pause = 0.0

    # --- evaluation ---------------------------------------------------------

    def check(self) -> None:
        """Evaluate the clock; emit tick, and expire when time runs out.

        Called automatically by the internal 100 ms ticker.  Also callable
        directly (tests) — no event loop required.
        """
        if self._state is not TimerState.RUNNING:
            return
        remaining = self.remaining_seconds()
        self.tick.emit(remaining)
        if remaining <= 0.0:
            self._state = TimerState.EXPIRED
            self._ticker.stop()
            logger.debug("Timer expired")
            self.expired.emit()