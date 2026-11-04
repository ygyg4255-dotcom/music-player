"""Tests for the island interface: validation, states, persistence, expansion."""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtGui
from PySide6.QtTest import QTest

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


# --- hover auto-expand / auto-collapse ----------------------------------------------


def _send(widget, event_type: QtCore.QEvent.Type) -> None:
    """Deliver a synthetic enter/leave event to *widget*."""
    QtCore.QCoreApplication.sendEvent(widget, QtCore.QEvent(event_type))


def _cursor_at(monkeypatch: pytest.MonkeyPatch, pos: QtCore.QPoint) -> None:
    monkeypatch.setattr(QtGui.QCursor, "pos", staticmethod(lambda: pos))


def _cursor_outside(monkeypatch: pytest.MonkeyPatch, island: IslandWindow) -> None:
    away = island.frameGeometry().topLeft() - QtCore.QPoint(500, 500)
    _cursor_at(monkeypatch, away)


def test_hover_expands_after_debounce(island: IslandWindow, monkeypatch):
    _cursor_at(monkeypatch, island.frameGeometry().center())
    assert island.is_expanded is False

    _send(island, QtCore.QEvent.Type.Enter)
    assert island._hover_expand_timer.isActive()  # debounce armed
    island._hover_expand()  # debounce elapsed with the mouse still inside

    assert island.is_expanded is True


def test_rapid_pass_through_does_not_expand(island: IslandWindow, monkeypatch):
    _cursor_at(monkeypatch, island.frameGeometry().center())
    _send(island, QtCore.QEvent.Type.Enter)
    _send(island, QtCore.QEvent.Type.Leave)  # left before the debounce fired
    assert island._hover_expand_timer.isActive() is False
    assert island.is_expanded is False


def test_hover_expanded_collapses_after_leave(island: IslandWindow, monkeypatch):
    _cursor_outside(monkeypatch, island)
    island._hover_expand()

    _send(island, QtCore.QEvent.Type.Leave)
    assert island._hover_collapse_timer.isActive()  # grace period armed
    island._hover_collapse()  # grace elapsed, cursor outside, no focus

    assert island.is_expanded is False
    assert island.height() == island._collapsed_height()
    assert island._frame.height() == island.PILL_HEIGHT


def test_leave_does_not_collapse_while_input_focused(
    island: IslandWindow, monkeypatch, qapp
):
    _cursor_outside(monkeypatch, island)
    island._hover_expand()
    island.song_input.setFocus()
    qapp.processEvents()
    assert island.song_input.hasFocus()

    _send(island, QtCore.QEvent.Type.Leave)
    island._hover_collapse()

    # Typing comfort wins: the panel stays open even if the cursor left.
    assert island.is_expanded is True


def test_leave_does_not_collapse_while_expired(
    island: IslandWindow, monkeypatch, clock: FakeClock
):
    _cursor_outside(monkeypatch, island)
    island._hover_expand()
    island.timer.start(5.0)
    clock.advance(6.0)
    island.timer.check()
    assert island.timer.state is TimerState.EXPIRED

    _send(island, QtCore.QEvent.Type.Leave)
    island._hover_collapse()

    # The expiry alert needs explicit dismissal; auto-collapse must not
    # hide it.
    assert island.is_expanded is True


def test_manual_expansion_also_collapses_on_leave(island: IslandWindow, monkeypatch):
    # Whatever started the expansion, moving the mouse away always shrinks
    # the island back into the slim compact bar.
    _cursor_outside(monkeypatch, island)
    island.expand_button.click()  # manual expand
    assert island.is_expanded is True

    _send(island, QtCore.QEvent.Type.Leave)
    island._hover_collapse()  # grace elapsed

    assert island.is_expanded is False
    assert island.height() == island._collapsed_height()
    assert island._frame.height() == island.PILL_HEIGHT


def test_toggle_button_still_collapses_hover_expansion(
    island: IslandWindow, monkeypatch, qapp
):
    _cursor_at(monkeypatch, island.frameGeometry().center())
    island._hover_expand()
    assert island.is_expanded is True

    island.expand_button.click()  # manual collapse on demand
    qapp.processEvents()
    assert island.is_expanded is False

    # Re-entering the compact pill arms a fresh hover expansion.
    _send(island, QtCore.QEvent.Type.Enter)
    assert island._hover_expand_timer.isActive()


def test_drag_press_blocks_hover_expand(island: IslandWindow, monkeypatch):
    _cursor_at(monkeypatch, island.frameGeometry().center())
    _send(island, QtCore.QEvent.Type.Enter)
    assert island._hover_expand_timer.isActive()

    # Pressing on the pill starts a window drag and must cancel the pending
    # hover expansion.
    QTest.mousePress(island, QtCore.Qt.MouseButton.LeftButton)
    assert island._hover_expand_timer.isActive() is False
    assert island._drag_pos is not None
    QTest.mouseRelease(island, QtCore.Qt.MouseButton.LeftButton)


# --- collapse geometry: window AND pill must actually shrink -----------------------


def test_button_collapse_shrinks_window_and_pill(island: IslandWindow, qapp):
    island.set_expanded(True, animate=False)
    qapp.processEvents()
    expanded_total = island.height()
    assert expanded_total > island._collapsed_height()
    assert island._frame.height() > island.PILL_HEIGHT

    island.expand_button.click()  # manual collapse (animated)
    QTest.qWait(320)  # let the 240 ms shrink animation finish

    assert island.is_expanded is False
    assert island.height() == island._collapsed_height()
    assert island._frame.height() == island.PILL_HEIGHT


def test_launch_expanded_collapses_on_mouse_in_out(monkeypatch, qapp):
    # The saved-settings launch state: the app starts expanded.  Moving the
    # mouse in and out must still collapse it into the slim bar.
    w = IslandWindow({"song": "x", "duration_minutes": 1.0, "expanded": True})
    w.show()
    qapp.processEvents()
    assert w.is_expanded is True
    assert w.height() > w._collapsed_height()

    # The mouse leaves the island (cursor outside) and the grace period
    # elapses — the expansion started from saved settings must not survive.
    _cursor_outside(monkeypatch, w)
    _send(w, QtCore.QEvent.Type.Enter)
    _send(w, QtCore.QEvent.Type.Leave)
    w._hover_collapse()  # grace period elapsed
    QTest.qWait(320)  # collapse is animated

    assert w.is_expanded is False
    assert w.height() == w._collapsed_height()
    assert w._frame.height() == w.PILL_HEIGHT
    w.close()


def test_animated_collapse_ends_at_compact_height(island: IslandWindow, qapp):
    island.set_expanded(True, animate=True)
    QTest.qWait(320)  # let the expand animation finish
    assert island.height() > island._collapsed_height()

    island.set_expanded(False, animate=True)  # e.g. the mouse-leave path
    QTest.qWait(320)

    assert island.height() == island._collapsed_height()
    assert island._frame.height() == island.PILL_HEIGHT


def test_collapse_keeps_top_edge_anchored(island: IslandWindow, qapp):
    island.set_expanded(True, animate=True)
    QTest.qWait(320)
    top = island.geometry().top()

    island.set_expanded(False, animate=True)
    QTest.qWait(320)

    # The slim bar must hang from the same top edge — the window must not
    # drift down (or up) while shrinking.
    assert island.geometry().top() == top


# --- close button -------------------------------------------------------------------


def test_close_button_present(island: IslandWindow):
    button = island.close_button
    assert button.isVisible()
    assert button.toolTip() == "Close Music Timer"
    assert (button.width(), button.height()) == (22, 22)
    # Cleanly separated from the expand toggle: it is the last header item.
    assert button is not island.expand_button


def test_close_button_hands_off_to_app_layer(island: IslandWindow, recorder):
    requests: list[int] = []
    island.close_requested.connect(lambda: requests.append(1))

    island.close_button.click()

    # First close: the island cleans up (settings persisted, countdown
    # cancelled) and hands the shutdown to the application layer — the
    # close is deferred so the tracked song can be paused first (verified
    # in the wiring tests).
    assert requests == [1]
    assert recorder.settings
    assert island.isVisible() is True  # not closed yet


def test_close_completes_after_app_layer_handoff(island: IslandWindow, qapp):
    # The app layer's role: like the pause handshake, its real close is
    # deferred — Qt ignores a close() issued from inside a closeEvent.
    island.close_requested.connect(
        lambda: QtCore.QTimer.singleShot(0, island.close)
    )
    island.close_button.click()
    assert island.isVisible() is True  # close deferred, not lost
    QTest.qWait(20)  # the deferred close completes the window
    assert island.isVisible() is False