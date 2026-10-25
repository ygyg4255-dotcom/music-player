"""Tests for the monotonic-clock CountdownTimer.

Most tests monkeypatch the timer module's ``monotonic`` and call ``check()``
directly, so the state machine is exercised deterministically with no
event loop and no real sleeping.  One real-time test verifies expiry
against the wall clock.
"""

from __future__ import annotations

import pytest
from PySide6.QtTest import QSignalSpy

from music_player import timer as timer_mod
from music_player.timer import CountdownTimer, TimerState


class FakeClock:
    """Deterministic replacement for time.monotonic."""

    def __init__(self) -> None:
        self.t = 10_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(timer_mod, "monotonic", fake)
    return fake


@pytest.fixture
def timer(clock: FakeClock) -> CountdownTimer:
    return CountdownTimer()


class Recorder:
    """Collects signal emissions as plain lists (no event loop needed)."""

    def __init__(self, timer: CountdownTimer) -> None:
        self.started: list[float] = []
        self.ticks: list[float] = []
        self.paused: list[float] = []
        self.resumed: list[float] = []
        self.cancelled: list[int] = []
        self.expired: list[int] = []
        timer.started.connect(self.started.append)
        timer.tick.connect(self.ticks.append)
        timer.paused.connect(self.paused.append)
        timer.resumed.connect(self.resumed.append)
        timer.cancelled.connect(lambda: self.cancelled.append(1))
        timer.expired.connect(lambda: self.expired.append(1))


# --- start ------------------------------------------------------------------


def test_start_sets_running_and_emits(timer: CountdownTimer, clock: FakeClock):
    rec = Recorder(timer)
    timer.start(90.0)
    assert timer.state is TimerState.RUNNING
    assert rec.started == [90.0]
    assert rec.ticks[-1] == pytest.approx(90.0)
    assert timer.remaining_seconds() == pytest.approx(90.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_duration_rejected(timer: CountdownTimer, bad: float):
    with pytest.raises(ValueError):
        timer.start(bad)
    assert timer.state is TimerState.IDLE


def test_remaining_is_computed_not_decremented(timer: CountdownTimer, clock: FakeClock):
    """Remaining must come from end - now, showing sub-second precision."""
    timer.start(600.0)
    clock.advance(0.5)
    assert timer.remaining_seconds() == pytest.approx(599.5)


# --- pause / resume ------------------------------------------------------------


def test_pause_stores_remaining(timer: CountdownTimer, clock: FakeClock):
    rec = Recorder(timer)
    timer.start(60.0)
    clock.advance(10.0)
    assert timer.pause() is True
    assert timer.state is TimerState.PAUSED
    assert rec.paused == [pytest.approx(50.0)]
    assert timer.remaining_seconds() == pytest.approx(50.0)


def test_pause_needs_running(timer: CountdownTimer):
    assert timer.pause() is False
    timer.start(30.0)
    assert timer.pause() is True
    assert timer.pause() is False  # already paused


def test_resume_continues_from_remaining(timer: CountdownTimer, clock: FakeClock):
    rec = Recorder(timer)
    timer.start(60.0)
    clock.advance(10.0)
    timer.pause()

    # Arbitrary wall-clock time passes while paused; the countdown must not.
    clock.advance(5_000.0)
    assert timer.resume() is True
    assert timer.state is TimerState.RUNNING
    assert rec.resumed[-1] == pytest.approx(50.0)
    assert timer.remaining_seconds() == pytest.approx(50.0)

    clock.advance(49.5)
    assert timer.remaining_seconds() == pytest.approx(0.5)
    clock.advance(0.5)
    timer.check()
    assert timer.state is TimerState.EXPIRED
    assert len(rec.expired) == 1


def test_resume_needs_paused(timer: CountdownTimer):
    assert timer.resume() is False
    timer.start(30.0)
    assert timer.resume() is False


# --- cancel / reset --------------------------------------------------------------


def test_cancel_from_running(timer: CountdownTimer):
    rec = Recorder(timer)
    timer.start(30.0)
    timer.cancel()
    assert timer.state is TimerState.IDLE
    assert rec.cancelled == [1]
    assert timer.remaining_seconds() == 0.0


def test_cancel_from_paused(timer: CountdownTimer, clock: FakeClock):
    rec = Recorder(timer)
    timer.start(30.0)
    timer.pause()
    timer.cancel()
    assert timer.state is TimerState.IDLE
    assert rec.cancelled == [1]


def test_cancel_from_idle_is_quiet(timer: CountdownTimer):
    rec = Recorder(timer)
    timer.cancel()
    assert rec.cancelled == []


def test_reset_from_expired(timer: CountdownTimer, clock: FakeClock):
    rec = Recorder(timer)
    timer.start(1.0)
    clock.advance(2.0)
    timer.check()
    assert timer.state is TimerState.EXPIRED
    timer.reset()
    assert timer.state is TimerState.IDLE
    assert rec.cancelled == []  # reset is silent, unlike cancel


# --- expiry ------------------------------------------------------------------------


def test_expiry_emits_exactly_once(timer: CountdownTimer, clock: FakeClock):
    rec = Recorder(timer)
    timer.start(60.0)
    clock.advance(59.0)
    timer.check()
    assert rec.expired == []
    clock.advance(1.5)
    timer.check()
    assert timer.state is TimerState.EXPIRED
    assert len(rec.expired) == 1
    timer.check()  # further evaluations must not re-emit
    timer.check()
    assert len(rec.expired) == 1


def test_check_is_noop_when_not_running(timer: CountdownTimer):
    rec = Recorder(timer)
    timer.check()
    assert rec.ticks == []


# --- real time ------------------------------------------------------------------------


def test_real_expiry_within_tolerance(qapp):
    """Real-clock test: a 0.4 s countdown expires within a sane window."""
    import time as _time

    t = CountdownTimer()
    spy = QSignalSpy(t.expired)
    t0 = _time.monotonic()
    t.start(0.4)
    assert spy.wait(5_000), "timer did not expire within 5 s"
    elapsed = _time.monotonic() - t0
    assert 0.35 <= elapsed <= 2.0, f"expired after {elapsed:.2f} s (expected ~0.4)"
    assert t.state is TimerState.EXPIRED