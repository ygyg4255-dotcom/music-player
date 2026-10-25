"""The floating "Dynamic Island" widget.

A compact, always-on-top, frameless, translucent pill with a drop shadow,
rounded corners, and a dark theme.  It has two layout states:

* collapsed — the pill itself: song, countdown, pause/cancel, expand button
* expanded  — additional controls appear below (song input, duration input,
  start button, status line)

State transitions animate smoothly.  All sizing is layout-driven: the
expand/collapse animation interpolates the window's minimum/maximum height
between the layout's own hints, so no widget is ever positioned by
coordinates.

The widget owns a :class:`~music_player.timer.CountdownTimer` for the
countdown but emits high-level signals (``start_requested`` etc.) so the
application layer can wire in local search, playback, and media control
(phases 3-4) without the UI knowing about them.
"""

from __future__ import annotations

import logging
import math

from PySide6 import QtCore, QtGui, QtWidgets

from . import theme
from .timer import CountdownTimer, TimerState

logger = logging.getLogger(__name__)


def _fmt_seconds(remaining: float) -> str:
    """Format remaining seconds as MM:SS (ceil, so 0:01 shows until expired)."""
    total = max(0, math.ceil(remaining))
    minutes, seconds = divmod(total, 60)
    return f"{minutes:02d}:{seconds:02d}"


class IslandWindow(QtWidgets.QWidget):
    """The compact always-on-top floating widget."""

    # --- wiring surface for the application layer --------------------------

    #: user asked to start a workflow: (song name, duration in minutes)
    start_requested = QtCore.Signal(str, float)
    #: countdown paused/resumed/cancelled by the user (payload: remaining s)
    countdown_paused = QtCore.Signal(float)
    countdown_resumed = QtCore.Signal(float)
    countdown_cancelled = QtCore.Signal()
    #: countdown reached zero — the app layer should pause playback
    countdown_expired = QtCore.Signal()
    #: countdown returned to idle after expiry dismissal (stop monitoring etc.)
    countdown_reset = QtCore.Signal()
    #: the user pressed Pause while the countdown runs — the app layer should
    #: also pause the selected song's media session (the countdown is already
    #: paused, with the exact remaining time, before this is emitted)
    playback_pause_requested = QtCore.Signal()
    #: the user pressed Resume on a paused countdown — the app layer resumes
    #: the selected song and resumes the countdown only after playback is
    #: confirmed Playing
    playback_resume_requested = QtCore.Signal()
    #: the user clicked the "start with Windows" checkbox (explicit opt-in)
    startup_toggled = QtCore.Signal(bool)
    #: UI settings changed (song text, duration, expanded) — app persists them
    ui_settings_changed = QtCore.Signal(dict)

    # --- geometry constants --------------------------------------------------
    PILL_HEIGHT = 58
    SHADOW_MARGIN = 16          # room around the pill for the drop shadow
    WINDOW_WIDTH = 400

    def __init__(self, stored: dict | None = None) -> None:
        super().__init__()
        self.setObjectName("islandWindow")
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool  # no taskbar entry
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        # Qt.Tool windows do not quit the app on close by default; without
        # this the process would linger invisibly in the background.
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_QuitOnClose, True)

        self._expanded = False
        self._anim_group: QtCore.QParallelAnimationGroup | None = None
        self._drag_pos: QtCore.QPoint | None = None

        self._timer = CountdownTimer(self)
        self._timer.started.connect(self._on_started)
        self._timer.tick.connect(self._on_tick)
        self._timer.paused.connect(self._on_paused)
        self._timer.resumed.connect(self._on_resumed)
        self._timer.cancelled.connect(self._on_cancelled)
        self._timer.expired.connect(self._on_expired)

        self._build_ui()
        self._restore(stored or {})
        self._update_view_state()
        if not self._expanded:  # _restore may already have expanded and sized us
            self._apply_geometry(self._collapsed_height(), animate=False)

    # --- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(
            self.SHADOW_MARGIN, self.SHADOW_MARGIN,
            self.SHADOW_MARGIN, self.SHADOW_MARGIN,
        )

        self._frame = QtWidgets.QFrame(objectName="island")
        outer.addWidget(self._frame)

        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 4)
        shadow.setColor(QtGui.QColor(0, 0, 0, 140))
        self._frame.setGraphicsEffect(shadow)

        root = QtWidgets.QVBoxLayout(self._frame)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(8)

        # -- header row (always visible) --
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(8)

        self._dot = QtWidgets.QLabel(objectName="statusDot")
        self._dot.setFixedSize(10, 10)
        header.addWidget(self._dot)

        self.title_label = QtWidgets.QLabel("What shall we play?")
        self.title_label.setObjectName("titleLabel")
        header.addWidget(self.title_label, 1)

        self.time_label = QtWidgets.QLabel("--:--")
        self.time_label.setObjectName("timeLabel")
        self.time_label.setMinimumWidth(78)
        self.time_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.time_label)

        self.pause_button = QtWidgets.QPushButton("⏸")
        self.pause_button.setObjectName("ghostButton")
        self.pause_button.setToolTip("Pause / resume the countdown")
        self.pause_button.setFixedSize(34, 30)
        self.pause_button.clicked.connect(self._on_pause_clicked)
        header.addWidget(self.pause_button)

        self.cancel_button = QtWidgets.QPushButton("✕")
        self.cancel_button.setObjectName("ghostButton")
        self.cancel_button.setToolTip("Cancel the countdown")
        self.cancel_button.setFixedSize(34, 30)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        header.addWidget(self.cancel_button)

        self.expand_button = QtWidgets.QPushButton("▾")
        self.expand_button.setObjectName("expandButton")
        self.expand_button.setToolTip("Show / hide controls")
        self.expand_button.setFixedSize(28, 30)
        self.expand_button.clicked.connect(
            lambda: self.set_expanded(not self._expanded)
        )
        header.addWidget(self.expand_button)

        root.addLayout(header)

        # -- expanded panel (animated in/out via height) --
        self._panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(0, 6, 0, 2)
        panel_layout.setSpacing(8)

        inputs = QtWidgets.QHBoxLayout()
        inputs.setSpacing(8)

        self.song_input = QtWidgets.QLineEdit()
        self.song_input.setPlaceholderText("Song name…")
        self.song_input.returnPressed.connect(self.start_workflow)
        self.song_input.setMinimumWidth(230)
        inputs.addWidget(self.song_input, 1)

        self.minutes_input = QtWidgets.QDoubleSpinBox()
        self.minutes_input.setRange(0.1, 10_080)
        self.minutes_input.setDecimals(1)
        self.minutes_input.setSuffix(" min")
        self.minutes_input.setValue(30.0)
        self.minutes_input.setMinimumWidth(90)
        inputs.addWidget(self.minutes_input)

        panel_layout.addLayout(inputs)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(8)

        self.start_button = QtWidgets.QPushButton("Start")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self.start_workflow)
        self.start_button.setMinimumWidth(90)
        actions.addWidget(self.start_button)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        actions.addWidget(self.status_label, 1)

        panel_layout.addLayout(actions)

        # -- Windows startup opt-in (explicit user permission required) --
        self.startup_checkbox = QtWidgets.QCheckBox("Start with Windows")
        self.startup_checkbox.setObjectName("startupCheckbox")
        self.startup_checkbox.setToolTip(
            "Run the music player automatically when you sign in (optional)."
        )
        # `clicked` (not `toggled`) fires only on user interaction, so
        # restoring the saved value never touches the Windows registry.
        # (PySide6 exposes clicked() and clicked(bool) overloads; reading
        # isChecked() keeps this independent of which one is delivered.)
        self.startup_checkbox.clicked.connect(
            lambda *_args: self.startup_toggled.emit(self.startup_checkbox.isChecked())
        )
        panel_layout.addWidget(self.startup_checkbox)

        root.addWidget(self._panel)

        self.setStyleSheet(theme.ISLAND_QSS)
        self._panel.hide()
        self.setFixedWidth(self.WINDOW_WIDTH)

    # --- geometry / expand / collapse ---------------------------------------

    def _collapsed_height(self) -> int:
        return self.PILL_HEIGHT + 2 * self.SHADOW_MARGIN

    def _expanded_height(self) -> int:
        """Height the layout wants when the panel is visible."""
        self._panel.show()
        hint = self.layout().sizeHint().height()
        return max(hint, self._collapsed_height())

    def _apply_geometry(self, height: int, animate: bool) -> None:
        """Pin the window to *height* (all width fixed), animated or not."""
        if self._anim_group is not None:
            self._anim_group.stop()
            self._anim_group.deleteLater()
            self._anim_group = None

        if not animate:
            self.setFixedHeight(height)
            return

        group = QtCore.QParallelAnimationGroup(self)
        for prop in (b"minimumHeight", b"maximumHeight"):
            anim = QtCore.QPropertyAnimation(self, prop, self)
            anim.setDuration(240)
            anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
            anim.setStartValue(self.height())
            anim.setEndValue(height)
            group.addAnimation(anim)
        group.finished.connect(group.deleteLater)
        group.start(QtCore.QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._anim_group = group

    def set_expanded(self, expanded: bool, animate: bool = True) -> None:
        """Expand or collapse the additional controls."""
        if expanded == self._expanded:
            return
        self._expanded = expanded
        target = self._expanded_height() if expanded else self._collapsed_height()
        if not expanded:
            # Hide only after the animation shrinks the window; guard against
            # a pending hide firing after a quick re-expand.
            def _hide_panel() -> None:
                if not self._expanded:
                    self._panel.hide()

            QtCore.QTimer.singleShot(260 if animate else 0, _hide_panel)
        self.expand_button.setText("▴" if expanded else "▾")
        self._apply_geometry(target, animate)
        self._emit_settings()

    # --- state ---------------------------------------------------------------

    @property
    def timer(self) -> CountdownTimer:
        """The countdown timer owned by this widget (exposed for tests/wiring)."""
        return self._timer

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    def current_settings(self) -> dict:
        """UI values to persist (song, duration, expanded, startup opt-in)."""
        return {
            "song": self.song_input.text().strip(),
            "duration_minutes": float(self.minutes_input.value()),
            "expanded": self._expanded,
            "start_with_windows": self.startup_checkbox.isChecked(),
        }

    def _emit_settings(self) -> None:
        self.ui_settings_changed.emit(self.current_settings())

    def _restore(self, stored: dict) -> None:
        song = str(stored.get("song") or "")
        if song:
            self.song_input.setText(song)
        try:
            minutes = float(stored.get("duration_minutes", 30.0))
            if 0.1 <= minutes <= 10_080:
                self.minutes_input.setValue(minutes)
        except (TypeError, ValueError):
            pass
        if stored.get("expanded"):
            self.set_expanded(True, animate=False)
        # Restored checkbox state must not re-emit startup_toggled: restore
        # via setChecked (toggled, not clicked) — no registry write happens.
        self.startup_checkbox.setChecked(bool(stored.get("start_with_windows", False)))

    # --- user actions ---------------------------------------------------------

    def start_workflow(self) -> None:
        """Validate inputs and start the countdown for the requested song."""
        song = self.song_input.text().strip()
        minutes = float(self.minutes_input.value())

        if not song:
            self._set_status("Enter a song name first.", error=True)
            self.song_input.setFocus()
            return
        if minutes <= 0:
            self._set_status("Duration must be positive.", error=True)
            return

        try:
            self._timer.cancel()  # allow a clean restart from any state
            self._timer.start(minutes * 60.0)
        except ValueError as exc:
            self._set_status(f"Invalid duration: {exc}", error=True)
            return

        self._set_status(f"Searching “{song}”…")
        self.start_requested.emit(song, minutes)
        self._emit_settings()
        self.set_expanded(False)  # shrink back to the compact pill

    def _on_pause_clicked(self) -> None:
        if self._timer.state is TimerState.RUNNING:
            # Immediate: the countdown pauses now with the exact remaining
            # time kept.  The app layer pauses the song's media session too,
            # so pausing the timer can never leave the song playing.
            self._timer.pause()
            self.playback_pause_requested.emit()
        elif self._timer.state is TimerState.PAUSED:
            # The countdown resumes only after playback is confirmed Playing
            # (the app layer resumes it via the media session).  Clicking ▶
            # never resumes the timer directly.
            self.playback_resume_requested.emit()
        elif self._timer.state is TimerState.EXPIRED:
            self._dismiss_expired()

    def _on_cancel_clicked(self) -> None:
        if self._timer.state is TimerState.EXPIRED:
            self._dismiss_expired()
        else:
            self._timer.cancel()

    def _dismiss_expired(self) -> None:
        self._timer.reset()
        self._set_status("")
        self._update_view_state()
        self.countdown_reset.emit()

    # --- timer reactions --------------------------------------------------------

    def _on_started(self, duration: float) -> None:
        self._update_view_state()

    def _on_tick(self, remaining: float) -> None:
        self.time_label.setText(_fmt_seconds(remaining))
        # Lightweight tooltip for precision without cluttering the pill.
        self.title_label.setToolTip(f"{max(0, int(math.ceil(remaining)))} s remaining")

    def _on_paused(self, remaining: float) -> None:
        self.countdown_paused.emit(remaining)
        self._set_status(f"Paused with {max(0, int(math.ceil(remaining)))} s left")
        self._update_view_state()

    def _on_resumed(self, remaining: float) -> None:
        self.countdown_resumed.emit(remaining)
        self._set_status("")
        self._update_view_state()

    def _on_cancelled(self) -> None:
        self.countdown_cancelled.emit()
        self._set_status("Cancelled")
        self._update_view_state()

    def _on_expired(self) -> None:
        logger.info("Countdown expired")
        self.countdown_expired.emit()
        self._set_status("Time's up — pausing playback")
        self._update_view_state()

    # --- visual state ------------------------------------------------------------

    _DOT_COLORS = {
        TimerState.IDLE: theme.BORDER,
        TimerState.RUNNING: theme.ACCENT,
        TimerState.PAUSED: "#ffd60a",
        TimerState.EXPIRED: theme.SUCCESS,
    }

    def _update_view_state(self) -> None:
        state = self._timer.state
        color = self._DOT_COLORS.get(state, theme.BORDER)
        self._dot.setStyleSheet(
            f"background: {color}; border-radius: 5px;"
        )

        if state is TimerState.IDLE:
            self.time_label.setText("--:--")
            self.pause_button.setVisible(False)
            self.cancel_button.setVisible(False)
            self.title_label.setText(
                "What shall we play?" if not self.song_input.text().strip()
                else self.song_input.text().strip()
            )
        elif state is TimerState.RUNNING:
            self.pause_button.setText("⏸")
            self.pause_button.setVisible(True)
            self.cancel_button.setVisible(True)
            self.title_label.setText(self.song_input.text().strip() or "Playing")
        elif state is TimerState.PAUSED:
            self.pause_button.setText("▶")
            self.pause_button.setVisible(True)
            self.cancel_button.setVisible(True)
            self.title_label.setText("Paused")
        elif state is TimerState.EXPIRED:
            self.pause_button.setVisible(False)
            self.cancel_button.setText("✓")
            self.cancel_button.setToolTip("Dismiss")
            self.cancel_button.setVisible(True)
            self.time_label.setText("00:00")
            self.title_label.setText("Time's up")

    def set_status(self, message: str, error: bool = False) -> None:
        """Public status line (used by the application layer)."""
        self._set_status(message, error)

    def _set_status(self, message: str, error: bool = False) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet(
            f"color: {theme.DANGER if error else theme.TEXT_DIM}; font-size: 11px;"
        )

    # --- dragging (frameless window) ---------------------------------------------

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._timer.cancel()
        self._emit_settings()
        super().closeEvent(event)