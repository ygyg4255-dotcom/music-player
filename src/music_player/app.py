"""Application wiring: lifecycle, playback pipeline, and media sync.

Everything the app does across its layers:

* **start workflow** — the island validates input and starts the countdown;
  the orchestrator searches the Music folder on a pool thread and either
  opens the local file with the default app or falls back to YouTube in
  the user's existing Brave browser.
* **playback gating** — from the moment Start is pressed the countdown is
  held paused (full duration intact) while the song is being found,
  opened, and loaded; it begins — with the *full* requested duration —
  only once the selected song's media session is confirmed *Playing*.
  Startup delays and autoplay restrictions never consume the user's
  duration.  Only the session whose media title matches the requested
  song is ever tracked, resumed, or paused.
* **user pause/resume** — the island's ⏸ pauses the countdown immediately
  (exact remaining time kept) and pauses the selected song's media
  session too; ▶ resumes the song and the countdown continues only after
  the session is confirmed *Playing*.  Failures are reported honestly and
  an unrelated session is never controlled as a fallback.
* **state sync** — while the countdown runs, the Windows media session of
  the relevant player is polled; a *manual* pause of that playback pauses
  the countdown too, and resuming continues from the remaining time.  No
  feedback loops: pausing we triggered ourselves (timer expiry) never
  re-enters the sync path.
* **expiry** — try to pause the source directly, then the system media
  session; report honestly when neither is possible.
* **startup opt-in** — the checkbox (off by default) is the explicit
  permission required to touch the Windows Run key.

* **close** — the first close request (✕, Alt+F4) is handed to the
  application layer: it hides the island immediately, cancels the
  countdown, and pauses the tracked song's media session with the exact
  same safe session-matching machinery as on expiry; the process exits
  when the pause attempt completes, capped at half a second so a stuck
  GSMTC call can never hang the exit.  When nothing was started, the
  app quits instantly.  A guard flag makes double-close harmless.

No system tray, no background service: when the island window closes, the
process exits and the media-control thread (daemon) dies with it.
"""

from __future__ import annotations

import logging
import sys

from PySide6 import QtCore, QtWidgets

from . import __version__ as VERSION
from . import config, settings, startup
from .island import IslandWindow
from .media_control import MediaController
from .playback import PlaybackError, PlaybackOrchestrator
from .timer import TimerState
from .youtube import YouTubeSource

logger = logging.getLogger(__name__)

#: Session statuses that mean "playback is not running" for sync purposes.
_NOT_PLAYING = {"Paused", "Stopped", "Closed"}


def _quit_app() -> None:
    """Quit the Qt event loop cleanly (kept as an indirection point for
    tests, which replace it to observe the quit without ending pytest)."""
    app = QtCore.QCoreApplication.instance()
    if app is not None:
        app.quit()


def _wire(
    window: IslandWindow,
    orchestrator: PlaybackOrchestrator,
    media: MediaController,
) -> None:
    """Connect the island, orchestrator, and media controller together."""

    # Persist song/duration/expanded/startup-optin on every UI change.
    window.ui_settings_changed.connect(lambda data: settings.save(data))

    orchestrator.status.connect(window.set_status)

    # --- workflow: local search → local file, else YouTube in Brave --------

    def _youtube_fallback(song: str) -> None:
        # Runs on a thread-pool thread (the orchestrator schedules it there):
        # play() fetches the YouTube search page to pick the first result,
        # so only queued-signal emissions are safe here — no direct GUI calls.
        source = YouTubeSource(song)
        try:
            source.play()
        except PlaybackError as exc:
            orchestrator.status.emit(str(exc), True)
            return
        orchestrator.report_source(source)  # → _on_source_started (queued)

    orchestrator.set_youtube_fallback(_youtube_fallback)

    def _on_source_started(source: object) -> None:
        # Track that player's media session for manual pause/resume sync.
        hint = getattr(source, "control_hint", None)
        title = getattr(source, "control_title", None)
        media.start_monitoring(hint, title_query=title)
        if getattr(source, "waits_for_playback", False):
            # FIX 2: hold the countdown — with its full remaining time —
            # until this song is confirmed *Playing*.  Start normally held
            # it already (see _on_start_requested); the pause() covers
            # paths that arrive here with the countdown running.
            if window.timer.state is TimerState.RUNNING:
                window.timer.pause()
            sync_paused["flag"] = True
            awaiting_playback["flag"] = True
            if not media.available:
                # Honest: without the media-session API there is no way to
                # confirm playback, so the countdown stays held.
                window.set_status(
                    "Windows media control is unavailable — playback cannot be "
                    "confirmed, so the countdown stays paused.",
                    True,
                )
                return
            # No recheck() here: start_monitoring() resets the status
            # baseline, so the session's first Playing report is emitted
            # naturally.  A forced re-emit could instead duplicate a report
            # that was already in flight — and a stale duplicate delivered
            # after a user pause wrongly resumes the countdown.
            window.set_status(
                getattr(source, "waiting_message", "Waiting for playback to start…"),
                False,
            )

    orchestrator.source_started.connect(_on_source_started)

    # --- playback-state → countdown synchronization -------------------------

    # True only while the countdown is paused *because playback was paused*.
    # A user-paused countdown (button) must never auto-resume.
    sync_paused = {"flag": False}
    # True while the countdown is paused *waiting for the YouTube playback to
    # start*: the user's duration must not be consumed until the selected
    # song is confirmed playing through the matching media session.
    awaiting_playback = {"flag": False}
    # A session not yet observed Playing is not "manually paused" — e.g. a
    # player still loading the file we just opened, or a stale session left
    # over from before this workflow.  Only a pause *after* a Playing
    # observation is treated as the user pausing the music.
    seen_playing = {"flag": False}

    def _on_start_requested(song: str, _minutes: float) -> None:
        # FIX 2: the countdown must not consume the user's duration while
        # the song is being found, opened, or loaded.  Hold it paused — the
        # full remaining time is kept — the moment Start is pressed; the
        # first confirmed *Playing* report starts it (see _on_tracked_state).
        if window.timer.state is TimerState.RUNNING and window.timer.pause():
            sync_paused["flag"] = True
            awaiting_playback["flag"] = True
        orchestrator.start(song)

    window.start_requested.connect(_on_start_requested)

    def _on_tracked_state(_source_id: str, status: str) -> None:
        timer = window.timer
        if status == "Playing":
            seen_playing["flag"] = True
        if timer.state is TimerState.RUNNING and status in _NOT_PLAYING and seen_playing["flag"]:
            sync_paused["flag"] = True
            timer.pause()
            window.set_status("Playback paused — countdown paused too.", False)
        elif timer.state is TimerState.PAUSED and sync_paused["flag"] and status == "Playing":
            sync_paused["flag"] = False
            if awaiting_playback["flag"]:
                # FIX 2: playback just confirmed — the countdown begins now
                # with the FULL requested duration (the user's five minutes
                # are still five minutes even if startup took eight seconds).
                awaiting_playback["flag"] = False
                timer.start(timer.duration_seconds)
                window.set_status("Playback confirmed — countdown running.", False)
            else:
                awaiting_playback["flag"] = False
                timer.resume()  # continues from the remaining time, no reset
                window.set_status("Playback resumed — countdown continuing.", False)

    media.tracked_state.connect(_on_tracked_state)

    def _on_countdown_resumed(_remaining: float) -> None:
        # Any resume (sync or user button) means the countdown is running on
        # the user's authority again — the waiting/sync-pause states end.
        sync_paused["flag"] = False
        awaiting_playback["flag"] = False

    window.countdown_resumed.connect(_on_countdown_resumed)

    def _on_session_lost(_source_id: str) -> None:
        if awaiting_playback["flag"]:
            # Playback never started (tab closed or autoplay blocked); the
            # user's duration must not run, so keep the countdown paused.
            window.set_status(
                "Waiting for the selected song to play — the countdown starts then.",
                False,
            )
            return
        if sync_paused["flag"]:
            sync_paused["flag"] = False
            # The player left; un-pause the countdown so it still completes.
            if window.timer.state is TimerState.PAUSED:
                window.timer.resume()
            window.set_status("Media session ended — countdown continues.", False)

    media.session_lost.connect(_on_session_lost)

    # --- user pause/resume of the countdown controls the selected song -------

    def _on_user_pause() -> None:
        # The island already paused the countdown — immediately, with the
        # exact remaining time.  FIX 1: the selected song's media session is
        # paused too, so pausing the timer can never leave the song playing
        # in the background.  An unrelated session is never a fallback.
        source = orchestrator.current_source
        if source is None:
            # Nothing this app started is playing (search still running, or
            # the workflow failed): there is no selected song to pause.
            window.set_status("Countdown paused — no song has been started to pause.", False)
            return
        # The upcoming Playing report (after a resume) may continue the
        # countdown — see _on_tracked_state.
        sync_paused["flag"] = True
        hint = getattr(source, "control_hint", None)
        title = getattr(source, "control_title", None)
        if media.available:
            media.pause_selected(hint, title_query=title)
        else:
            window.set_status(
                "Windows media control is unavailable — only the countdown was paused.",
                True,
            )

    window.playback_pause_requested.connect(_on_user_pause)

    def _on_user_resume() -> None:
        # FIX 1: resume the selected song; the countdown resumes only after
        # the session is confirmed *Playing* (_on_tracked_state), never on
        # the click itself.
        source = orchestrator.current_source
        if source is None:
            window.set_status(
                "No song has been started — the countdown stays paused.", True
            )
            return
        hint = getattr(source, "control_hint", None)
        title = getattr(source, "control_title", None)
        if media.available:
            media.resume_selected(hint, title_query=title)
            # A session that is Playing again already (pause refused, or the
            # user resumed in the player) would not re-emit — force one report
            # so the countdown is never left paused on a playing song.
            media.recheck()
        else:
            window.set_status(
                "Windows media control is unavailable — the countdown stays paused.",
                True,
            )

    window.playback_resume_requested.connect(_on_user_resume)

    def _on_control_result(_source_id: str, ok: bool, message: str) -> None:
        # Honest outcome of the user-initiated pause/resume attempt.
        window.set_status(message, not ok)

    media.control_result.connect(_on_control_result)

    # --- expiry: pause whatever this app started, or the relevant session ---

    def _on_expired() -> None:
        source, paused = orchestrator.pause_playback()
        if source is not None and paused:
            window.set_status(f"Time's up — paused {source.capabilities.label}.", False)
            return
        if media.available:
            hint = source.control_hint if source is not None else None
            # The title query makes sure only the selected song's session is
            # paused — never an unrelated Brave tab or another player.
            title = source.control_title if source is not None else None
            media.pause_relevant(hint, title_query=title)
        elif source is not None:
            window.set_status(
                f"Time's up — {source.capabilities.label} cannot be paused automatically; "
                "Windows media control is unavailable.",
                True,
            )
        else:
            window.set_status(
                "Time's up — nothing was started through the app.", True
            )

    window.countdown_expired.connect(_on_expired)

    def _on_pause_result(_source_id: str, ok: bool, message: str) -> None:
        window.set_status(f"Time's up — {message}", not ok)

    media.pause_result.connect(_on_pause_result)

    # --- countdown lifecycle → monitoring lifecycle ---------------------------

    def _stop_monitoring() -> None:
        sync_paused["flag"] = False
        awaiting_playback["flag"] = False
        media.stop_monitoring()

    window.countdown_cancelled.connect(_stop_monitoring)
    window.countdown_reset.connect(_stop_monitoring)

    # --- Windows startup opt-in (explicit user permission) ---------------------

    def _on_startup_toggled(checked: bool) -> None:
        if checked:
            error = startup.enable()
            if error:
                window.set_status(error, True)
                window.startup_checkbox.setChecked(False)  # revert; no signal loop
            else:
                window.set_status("The music player will start with Windows.", False)
        else:
            error = startup.disable()
            window.set_status(
                error or "Windows startup registration removed.", bool(error)
            )

    window.startup_toggled.connect(_on_startup_toggled)

    # --- close: pause the tracked song before the process exits -------------

    # Re-entrancy guard: a second close request (double ✕ click, or a
    # closeEvent after the handshake began) must never start a second one.
    closing = {"flag": False}
    finished = {"flag": False}
    # True only while _finish_shutdown is connected to pause_result; PySide6
    # warns on disconnecting a slot that was never connected.
    pause_wait = {"flag": False}

    def _finish_shutdown(*_args: object) -> None:
        """End the process once the pause attempt completed (or timed out)."""
        if finished["flag"]:
            return
        finished["flag"] = True
        if pause_wait["flag"]:
            media.pause_result.disconnect(_finish_shutdown)
            pause_wait["flag"] = False
        media.stop_monitoring()
        _quit_app()

    def _on_close_requested() -> None:
        if closing["flag"]:
            return
        closing["flag"] = True
        window.hide()  # the UI feels instant
        window.timer.cancel()  # the countdown never ticks during shutdown
        source = orchestrator.current_source
        if source is None:
            # Idle (or only a search still in flight): nothing this app
            # started can be playing, so no pause is needed — quit now.
            _finish_shutdown()
            return
        # Pause the tracked song with the exact same safe machinery as on
        # expiry: try the source directly, then the GSMTC session matched
        # by app hint + song title; an unrelated session is never touched.
        source, paused = orchestrator.pause_playback()
        if source is not None and paused:
            _finish_shutdown()
            return
        if media.available:
            # pause_relevant() runs on the media-control daemon thread;
            # its outcome arrives via pause_result — but exiting the event
            # loop on this very tick would kill that thread mid-call, so
            # wait (bounded) for the result before quitting.
            pause_wait["flag"] = True
            media.pause_result.connect(_finish_shutdown)
            media.pause_relevant(source.control_hint, title_query=source.control_title)
            # Never hang: cap the wait even if GSMTC never answers.
            QtCore.QTimer.singleShot(500, _finish_shutdown)
        else:
            # Honest: without the media-session API there is nothing to
            # pause; exiting immediately is the only correct thing to do.
            _finish_shutdown()

    window.close_requested.connect(_on_close_requested)


def _configure_logging() -> None:
    """Console logging, plus a log file for windowed/packaged runs.

    The file lives next to the settings file under %LOCALAPPDATA% so even
    the --noconsole executable leaves diagnostics behind.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        log_dir = config.settings_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "music-player.log", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(handler)
    except OSError as exc:
        logging.getLogger(__name__).warning("File logging unavailable: %s", exc)


def run(argv: list[str] | None = None, quit_after_ms: int | None = None) -> int:
    """Create the application and start the event loop.  Returns exit code.

    ``quit_after_ms`` closes the window automatically after the given delay.
    It is only used by automated launch verification and tests; normal runs
    leave it unset.
    """
    _configure_logging()
    logger.info("Starting %s v%s", config.APP_NAME, VERSION)

    QtCore.QCoreApplication.setOrganizationName("DynamicIslandMusicPlayer")
    QtCore.QCoreApplication.setApplicationName(config.APP_NAME)
    QtCore.QCoreApplication.setApplicationVersion(VERSION)

    app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    app.setQuitOnLastWindowClosed(True)  # closing the island ends the process

    orchestrator = PlaybackOrchestrator(app)
    media = MediaController(app)
    logger.info(
        "Windows media-session control available: %s (winrt %s)",
        media.available,
        "installed" if media.available else "missing",
    )
    window = IslandWindow(settings.load())
    _wire(window, orchestrator, media)

    # Top-center placement on the primary screen.
    screen = QtWidgets.QApplication.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        x = geo.center().x() - window.width() // 2
        window.move(x, geo.top() + 24)

    window.show()

    if quit_after_ms is not None:
        # Automated verification: close the window after the delay; with
        # quitOnLastWindowClosed this ends the event loop and the process.
        QtCore.QTimer.singleShot(quit_after_ms, window.close)

    code = 1
    try:
        code = app.exec()
    finally:
        media.stop_monitoring()
        logger.info("Exited with code %s", code)
    return code