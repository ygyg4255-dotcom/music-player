"""Tests for the island interface: validation, states, persistence, expansion."""

from __future__ import annotations

import pytest
from PySide6 import QtCore

from music_player import timer as timer_mod
from music_player.island import IslandWindow
from music_player.timer import TimerState


class FakeClock:
    def __init__(self) -> None:
        self.t = 5_000.0

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
def island(qapp, clock: FakeClock) -> IslandWindow:
    w = IslandWindow()
    w.show()
    yield w
    w.close()


@pytest.fixture
def recorder(island: IslandWindow):
    class Rec:
        def __init__(self) -> None:
            self.starts: list[tuple] = []
            self.paused: list[float] = []
            self.resumed: list[float] = []
            self.cancelled: list[int] = []
            self.expired: list[int] = []
            self.settings: list[dict] = []

    rec = Rec()
    island.start_requested.connect(lambda s, m: rec.starts.append((s, m)))
    island.countdown_paused.connect(rec.paused.append)
    island.countdown_resumed.connect(rec.resumed.append)
    island.countdown_cancelled.connect(lambda: rec.cancelled.append(1))
    island.countdown_expired.connect(lambda: rec.expired.append(1))
    island.ui_settings_changed.connect(rec.settings.append)
    return rec


# --- input validation -------------------------------------------------------


def test_empty_song_rejected(island: IslandWindow, recorder):
    island.song_input.clear()
    island.start_workflow()
    assert island.timer.state is TimerState.IDLE
    assert recorder.starts == []
    assert island.status_label.text() != ""  # error shown, no crash


def test_zero_duration_rejected(island: IslandWindow, recorder, monkeypatch):
    monkeypatch.setattr(island.minutes_input, "value", lambda: 0.0)
    island.song_input.setText("anything")
    island.start_workflow()
    assert island.timer.state is TimerState.IDLE
    assert recorder.starts == []


def test_whitespace_song_rejected(island: IslandWindow, recorder):
    island.song_input.setText("    ")
    island.start_workflow()
    assert island.timer.state is TimerState.IDLE
    assert recorder.starts == []


# --- start workflow ------------------------------------------------------------


def test_valid_start_emits_and_runs(island: IslandWindow, recorder):
    island.song_input.setText("Karma Police")
    island.minutes_input.setValue(2.5)
    island.start_workflow()

    assert recorder.starts == [("Karma Police", 2.5)]
    assert island.timer.state is TimerState.RUNNING
    assert island.time_label.text() == "02:30"
    assert island.is_expanded is False  # collapsed back to the pill


def test_restart_while_running(island: IslandWindow, recorder, clock: FakeClock):
    island.song_input.setText("song a")
    island.start_workflow()
    clock.advance(60)
    island.song_input.setText("song b")
    island.minutes_input.setValue(1)
    island.start_workflow()

    assert island.timer.state is TimerState.RUNNING
    assert island.timer.remaining_seconds() == pytest.approx(60.0)
    assert recorder.starts[-1] == ("song b", 1.0)


# --- pause / resume / cancel / expire ----------------------------------------------


def test_pause_resume_buttons(island: IslandWindow, recorder):
    island.song_input.setText("song")
    island.start_workflow()

    pause_requests: list[int] = []
    resume_requests: list[int] = []
    island.playback_pause_requested.connect(lambda: pause_requests.append(1))
    island.playback_resume_requested.connect(lambda: resume_requests.append(1))

    # ⏸ pauses the countdown immediately (exact remaining kept) and asks the
    # app layer to pause the selected song's media session too.
    island.pause_button.click()
    assert island.timer.state is TimerState.PAUSED
    assert len(recorder.paused) == 1
    assert len(pause_requests) == 1
    assert island.pause_button.text() == "▶"

    # ▶ no longer resumes the countdown directly: it asks the app layer to
    # resume the selected song — the countdown resumes only after playback
    # is confirmed Playing (verified at the wiring level).
    island.pause_button.click()
    assert island.timer.state is TimerState.PAUSED
    assert len(recorder.resumed) == 0
    assert len(resume_requests) == 1


def test_cancel_button_returns_to_idle(island: IslandWindow, recorder):
    island.song_input.setText("song")
    island.start_workflow()
    island.cancel_button.click()

    assert island.timer.state is TimerState.IDLE
    assert recorder.cancelled == [1]
    assert island.pause_button.isVisible() is False


def test_countdown_shows_sub_second_accuracy(island: IslandWindow, clock: FakeClock):
    island.song_input.setText("song")
    island.minutes_input.setValue(1)
    island.start_workflow()
    clock.advance(30.4)
    island.timer.check()
    # 29.6 s left -> ceil display 30 s -> "00:30"
    assert island.time_label.text() == "00:30"


def test_expiry_updates_view_and_can_be_dismissed(island: IslandWindow, recorder, clock: FakeClock):
    island.song_input.setText("song")
    island.start_workflow()

    clock.advance(island.timer.duration_seconds + 1)
    island.timer.check()

    assert island.timer.state is TimerState.EXPIRED
    assert recorder.expired == [1]
    assert island.title_label.text() == "Time's up"
    assert island.cancel_button.text() == "✓"

    island.cancel_button.click()  # dismiss
    assert island.timer.state is TimerState.IDLE
    assert island.time_label.text() == "--:--"


# --- settings persistence ------------------------------------------------------------


def test_settings_restored_into_inputs(qapp):
    w = IslandWindow({"song": "Street Spirit", "duration_minutes": 5.0, "expanded": False})
    assert w.song_input.text() == "Street Spirit"
    assert w.minutes_input.value() == 5.0
    w.close()


def test_bad_restored_settings_do_not_crash(qapp):
    w = IslandWindow({"song": 123, "duration_minutes": "nope", "expanded": None})
    # Falls back to sane values instead of crashing.
    assert w.minutes_input.value() == 30.0
    w.close()


def test_ui_reports_settings_for_persistence(island: IslandWindow, recorder):
    island.song_input.setText("No Surprises")
    island.minutes_input.setValue(3)
    island.start_workflow()

    reported = recorder.settings[-1]
    assert reported["song"] == "No Surprises"
    assert reported["duration_minutes"] == 3.0


# --- expand / collapse ------------------------------------------------------------------


def test_expand_collapse_changes_height(island: IslandWindow, qapp):
    collapsed = island.height()
    island.set_expanded(True, animate=False)
    qapp.processEvents()
    assert island.is_expanded is True
    assert island.height() > collapsed

    island.set_expanded(False, animate=False)
    qapp.processEvents()
    assert island.is_expanded is False
    assert island.height() == collapsed


def test_settings_restore_expanded(qapp):
    w = IslandWindow({"song": "x", "duration_minutes": 1.0, "expanded": True})
    assert w.is_expanded is True
    w.close()