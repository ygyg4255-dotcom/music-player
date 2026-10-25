"""Integration tests for the app wiring: playback-state ↔ countdown sync.

These drive the real `_wire()` connections with a fake media backend and a
monkeypatched clock, verifying the phase-4 synchronization rules:

* manual playback pause → countdown pauses;
* playback resume → countdown continues from the remaining time;
* user-paused countdown is never auto-resumed;
* expiry attempts the system-level pause and reports honestly;
* cancel/dismiss stops media monitoring;
* the startup checkbox is the explicit permission for registry changes.
"""

from __future__ import annotations

import time

import pytest

from music_player import app as app_module
from music_player import local_search
from music_player.app import _wire
from music_player.island import IslandWindow
from music_player.media_control import MediaController, SessionInfo
from music_player.playback import PlaybackOrchestrator
from music_player.timer import TimerState

POT = SessionInfo("PotPlayerMini64.exe", "Playing")
POT_PAUSED = SessionInfo("PotPlayerMini64.exe", "Paused")


class FakeClock:
    def __init__(self) -> None:
        self.t = 5_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeBackend:
    def __init__(self) -> None:
        self.sessions: list[SessionInfo] = []
        self.pause_calls: list = []
        self.play_calls: list = []

    async def get_sessions(self) -> list[SessionInfo]:
        return list(self.sessions)

    async def pause(self, source_id: str, title: str = "") -> bool:
        self.pause_calls.append((source_id, title))
        return True

    async def play(self, source_id: str, title: str = "") -> bool:
        self.play_calls.append((source_id, title))
        return True


class StubSettings:
    saved: list[dict] = []

    @classmethod
    def save(cls, data: dict) -> None:
        cls.saved.append(data)

    @classmethod
    def load(cls) -> dict:
        return {}


class StubStartup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def enable(self) -> str | None:
        self.calls.append("enable")
        return None

    def disable(self) -> str | None:
        self.calls.append("disable")
        return None


def _pump(qapp, condition, timeout_ms: int = 3_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while not condition():
        if time.monotonic() > deadline:
            return False
        qapp.processEvents()
        time.sleep(0.01)
    return True


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    import music_player.timer as timer_mod

    fake = FakeClock()
    monkeypatch.setattr(timer_mod, "monotonic", fake)
    return fake


@pytest.fixture
def wired(qapp, clock, monkeypatch):
    """A fully wired island + orchestrator + fake media controller."""
    backend = FakeBackend()
    media = MediaController(backend_factory=lambda: backend, poll_interval_s=0.02)
    orch = PlaybackOrchestrator()
    monkeypatch.setattr(app_module, "settings", StubSettings)
    stub_startup = StubStartup()
    monkeypatch.setattr(app_module, "startup", stub_startup)
    window = IslandWindow()
    _wire(window, orch, media)
    window.show()
    yield type("W", (), {"window": window, "orch": orch, "media": media,
                         "backend": backend, "startup": stub_startup})()
    window.close()
    media._shutdown()


# --- playback-state → countdown synchronization -------------------------------


def test_stale_paused_session_does_not_pause_countdown(wired, qapp, clock):
    """Regression: a leftover session reporting 'Paused' before our file
    starts playing is NOT a manual pause (observed live with a suspended
    UWP player holding an old paused session)."""
    wired.window.timer.start(120)
    # Monitor starts while the session still holds its old Paused state…
    wired.backend.sessions = [POT_PAUSED]
    wired.media.start_monitoring("potplayermini64")
    assert _pump(qapp, lambda: wired.media._tracked_id == POT.source_id)

    clock.advance(5)
    wired.window.timer.check()
    assert wired.window.timer.state is TimerState.RUNNING  # no sync pause

    # …and only after playback is observed may a pause pause the countdown.
    wired.backend.sessions = [POT]
    assert _pump(qapp, lambda: wired.media._last_status == "Playing")
    wired.backend.sessions = [POT_PAUSED]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.PAUSED)


def test_manual_playback_pause_pauses_countdown(wired, qapp, clock):
    wired.window.timer.start(120)
    wired.backend.sessions = [POT]
    wired.media.start_monitoring("potplayermini64")
    assert _pump(qapp, lambda: wired.media._tracked_id == POT.source_id)

    # The user pauses the music in the player itself…
    wired.backend.sessions = [POT_PAUSED]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.PAUSED)
    assert wired.window.timer.remaining_seconds() == 120.0  # remaining kept


def test_playback_resume_continues_from_remaining(wired, qapp, clock):
    wired.window.timer.start(120)
    wired.backend.sessions = [POT]
    wired.media.start_monitoring("potplayermini64")
    assert _pump(qapp, lambda: wired.media._tracked_id == POT.source_id)

    wired.backend.sessions = [POT_PAUSED]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.PAUSED)

    clock.advance(90)  # wall-clock time passes while paused: no countdown loss
    wired.backend.sessions = [POT]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)
    assert wired.window.timer.remaining_seconds() == pytest.approx(120.0)

    # And the countdown keeps running afterwards.
    clock.advance(10)
    wired.window.timer.check()
    assert wired.window.timer.remaining_seconds() == pytest.approx(110.0)


def test_user_paused_countdown_is_not_auto_resumed(wired, qapp):
    wired.window.timer.start(120)
    wired.backend.sessions = [POT]
    wired.media.start_monitoring("potplayermini64")
    assert _pump(qapp, lambda: wired.media._tracked_id == POT.source_id)

    wired.window.pause_button.click()  # user pauses the *countdown* only
    assert wired.window.timer.state is TimerState.PAUSED

    # Session stays/becomes Playing — must NOT resurrect the countdown.
    wired.backend.sessions = [POT]
    time.sleep(0.1)
    qapp.processEvents()
    qapp.processEvents()
    assert wired.window.timer.state is TimerState.PAUSED


def test_session_loss_resumes_sync_paused_countdown(wired, qapp, clock):
    wired.window.timer.start(120)
    wired.backend.sessions = [POT]
    wired.media.start_monitoring("potplayermini64")
    assert _pump(qapp, lambda: wired.media._last_status == "Playing")  # playing observed
    wired.backend.sessions = [POT_PAUSED]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.PAUSED)

    wired.backend.sessions = []  # player closed entirely
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)


# --- expiry pauses via the system media session ------------------------------


def test_expiry_pauses_relevant_media_session(wired, qapp, clock):
    wired.window.timer.start(60)
    wired.backend.sessions = [POT]
    wired.media.start_monitoring("potplayermini64")
    assert _pump(qapp, lambda: wired.media._tracked_id == POT.source_id)

    clock.advance(61)
    wired.window.timer.check()
    assert wired.window.timer.state is TimerState.EXPIRED

    assert _pump(qapp, lambda: bool(wired.backend.pause_calls))
    assert wired.backend.pause_calls == [(POT.source_id, "")]
    assert _pump(qapp, lambda: "Paused" in wired.window.status_label.text())
    # Expiry pause must not loop back into the countdown (timer is EXPIRED).
    wired.backend.sessions = [POT_PAUSED]
    time.sleep(0.1)
    qapp.processEvents()
    assert wired.window.timer.state is TimerState.EXPIRED


# --- monitoring lifecycle -------------------------------------------------------


def test_cancel_stops_media_monitoring(wired, qapp):
    wired.window.timer.start(120)
    wired.backend.sessions = [POT]
    wired.media.start_monitoring("potplayermini64")
    assert wired.media._monitoring is True

    wired.window.cancel_button.click()
    assert wired.window.timer.state is TimerState.IDLE
    assert wired.media._monitoring is False


# --- Windows startup opt-in wiring ----------------------------------------------


def test_startup_checkbox_enables_registration(wired):
    wired.window.startup_checkbox.click()  # user ticks the box
    assert wired.startup.calls == ["enable"]
    assert wired.window.startup_checkbox.isChecked() is True


def test_startup_error_reverts_checkbox(wired, monkeypatch):
    def failing_enable():
        wired.startup.calls.append("enable")
        return "registry denied"

    wired.startup.enable = failing_enable
    wired.window.startup_checkbox.click()
    assert wired.startup.calls == ["enable"]
    assert wired.window.startup_checkbox.isChecked() is False  # reverted
    assert "registry denied" in wired.window.status_label.text()


def test_startup_uncheck_disables_registration(wired):
    wired.window.startup_checkbox.click()   # enable
    wired.window.startup_checkbox.click()    # disable
    assert wired.startup.calls == ["enable", "disable"]


def test_restore_does_not_touch_registry(qapp, monkeypatch):
    """Restoring a saved checkbox state must never write the registry."""
    import music_player.startup as real_startup

    calls = []
    monkeypatch.setattr(real_startup, "enable", lambda *a, **k: calls.append("enable"))
    monkeypatch.setattr(real_startup, "disable", lambda *a, **k: calls.append("disable"))

    window = IslandWindow({"start_with_windows": True})
    assert window.startup_checkbox.isChecked() is True
    window.close()
    assert calls == []  # pure restore — the registry is untouched


# --- YouTube fallback wiring -------------------------------------------------------

BRAVE_SONG = SessionInfo("BraveBrowser.XYZ", "Playing", title="Radiohead - Karma Police")
BRAVE_SONG_PAUSED = SessionInfo("BraveBrowser.XYZ", "Paused", title="Radiohead - Karma Police")
BRAVE_OTHER = SessionInfo("BraveBrowser.XYZ", "Playing", title="Some Other Video")
SPOTIFY_OTHER = SessionInfo("Spotify.exe", "Playing", title="Totally Different Song")


def _stub_youtube(monkeypatch, played: list) -> None:
    """Replace YouTubeSource with a no-network stub that waits for playback."""

    class StubYouTubeSource:
        def __init__(self, song):
            self.song = song

        def play(self):
            played.append(self.song)

        def pause(self):
            return False

        control_hint = "brave"
        capabilities = type("C", (), {"label": "YouTube in Brave"})()
        waits_for_playback = True
        waiting_message = "YouTube: the countdown starts when the song plays."

        @property
        def control_title(self):
            return self.song

    monkeypatch.setattr(app_module, "YouTubeSource", StubYouTubeSource)


def test_youtube_fallback_reports_source_and_starts_monitoring(wired, qapp, monkeypatch):
    played = []
    _stub_youtube(monkeypatch, played)
    wired.window.timer.start(120)
    wired.orch._youtube_fallback("some song")

    assert played == ["some song"]
    assert wired.orch.current_source is not None
    assert wired.media._monitoring is True
    assert wired.media._hint == "brave"
    assert wired.media._title_query == "some song"
    # The countdown is gated (paused at its full remaining time) until the
    # selected song is confirmed playing — no duration is consumed before.
    assert wired.window.timer.state is TimerState.PAUSED
    assert wired.window.timer.remaining_seconds() == 120.0
    assert "YouTube" in wired.window.status_label.text()
    wired.media.stop_monitoring()  # cleanup for the next test


def test_youtube_fallback_error_is_reported(wired, monkeypatch):
    from music_player.playback import PlaybackError

    class FailingSource:
        def __init__(self, song):
            pass

        def play(self):
            raise PlaybackError("no browser")

        control_hint = "brave"

    monkeypatch.setattr(app_module, "YouTubeSource", FailingSource)
    wired.orch._youtube_fallback("some song")
    assert "no browser" in wired.window.status_label.text()
    assert wired.orch.current_source is None


# --- YouTube playback gating ↔ countdown synchronization ----------------------------


def test_youtube_wait_holds_countdown_until_playing(wired, qapp, clock, monkeypatch):
    """No Playing session → the countdown must not consume any duration."""
    played = []
    _stub_youtube(monkeypatch, played)
    wired.window.timer.start(120)
    wired.orch._youtube_fallback("karma police")

    assert wired.window.timer.state is TimerState.PAUSED
    clock.advance(30)  # seconds pass while waiting: nothing may be consumed
    wired.window.timer.check()
    assert wired.window.timer.remaining_seconds() == 120.0

    # The selected song's session appears and plays → countdown resumes and
    # continues from the full remaining time, exactly.
    wired.backend.sessions = [BRAVE_SONG]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)
    assert wired.window.timer.remaining_seconds() == pytest.approx(120.0)

    clock.advance(10)
    wired.window.timer.check()
    assert wired.window.timer.remaining_seconds() == pytest.approx(110.0)


def test_youtube_wait_ignores_unrelated_playing_session(wired, qapp, clock, monkeypatch):
    """A Brave tab playing something else must never start the countdown."""
    _stub_youtube(monkeypatch, [])
    wired.window.timer.start(120)
    wired.orch._youtube_fallback("karma police")

    wired.backend.sessions = [BRAVE_OTHER, SPOTIFY_OTHER]  # unrelated media
    time.sleep(0.2)
    qapp.processEvents()
    qapp.processEvents()
    assert wired.window.timer.state is TimerState.PAUSED  # duration not consumed
    assert wired.window.timer.remaining_seconds() == 120.0
    assert wired.media._tracked_id is None  # never tracked, never controlled


def test_youtube_pause_resumes_from_exact_remaining(wired, qapp, clock, monkeypatch):
    _stub_youtube(monkeypatch, [])
    wired.window.timer.start(120)
    wired.orch._youtube_fallback("karma police")
    wired.backend.sessions = [BRAVE_SONG]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)

    clock.advance(20)
    wired.window.timer.check()
    wired.backend.sessions = [BRAVE_SONG_PAUSED]  # the user pauses the video
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.PAUSED)
    assert wired.window.timer.remaining_seconds() == pytest.approx(100.0)

    clock.advance(90)  # wall-clock time passes while paused: nothing lost
    wired.backend.sessions = [BRAVE_SONG]  # the user resumes the video
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)
    assert wired.window.timer.remaining_seconds() == pytest.approx(100.0)


def test_youtube_expiry_pauses_only_selected_song(wired, qapp, clock, monkeypatch):
    """Expiry must pause the session of the selected song — never another
    playing Brave tab or any unrelated player."""
    _stub_youtube(monkeypatch, [])
    wired.window.timer.start(60)
    wired.orch._youtube_fallback("karma police")
    wired.backend.sessions = [BRAVE_SONG, BRAVE_OTHER, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)

    clock.advance(61)
    wired.window.timer.check()
    assert wired.window.timer.state is TimerState.EXPIRED

    assert _pump(qapp, lambda: bool(wired.backend.pause_calls))
    assert wired.backend.pause_calls == [(BRAVE_SONG.source_id, "Radiohead - Karma Police")]
    assert _pump(qapp, lambda: "Paused" in wired.window.status_label.text())


def test_youtube_session_loss_while_waiting_keeps_duration(wired, qapp, clock, monkeypatch):
    """The tab closes before playback starts: no Playing state, no countdown."""
    _stub_youtube(monkeypatch, [])
    wired.window.timer.start(120)
    wired.orch._youtube_fallback("karma police")
    wired.backend.sessions = [BRAVE_SONG_PAUSED]  # autoplay blocked: paused tab
    assert _pump(qapp, lambda: wired.media._tracked_id is not None)

    wired.backend.sessions = []  # the user closes the tab before playing
    assert _pump(
        qapp,
        lambda: "countdown starts then" in wired.window.status_label.text(),
    )
    assert wired.window.timer.state is TimerState.PAUSED
    clock.advance(60)
    wired.window.timer.check()
    assert wired.window.timer.remaining_seconds() == 120.0  # fully preserved


def test_youtube_autoplay_blocked_consumes_no_duration(wired, qapp, clock, monkeypatch):
    """Autoplay blocked: the tab stays paused forever — the countdown must
    stay gated (not RUNNING, not EXPIRED) with the full duration intact."""
    _stub_youtube(monkeypatch, [])
    wired.window.timer.start(120)
    wired.orch._youtube_fallback("karma police")

    wired.backend.sessions = [BRAVE_SONG_PAUSED]  # loaded but never plays
    assert _pump(qapp, lambda: wired.media._tracked_id is not None)
    time.sleep(0.1)
    qapp.processEvents()

    clock.advance(300)  # five minutes of wall-clock time pass
    wired.window.timer.check()
    assert wired.window.timer.state is TimerState.PAUSED
    assert wired.window.timer.remaining_seconds() == 120.0
    assert "countdown starts" in wired.window.status_label.text()


def test_cancel_during_youtube_wait_resets_state(wired, qapp, monkeypatch):
    _stub_youtube(monkeypatch, [])
    wired.window.timer.start(120)
    wired.orch._youtube_fallback("karma police")
    assert wired.window.timer.state is TimerState.PAUSED

    wired.window.cancel_button.click()
    assert wired.window.timer.state is TimerState.IDLE
    assert wired.media._monitoring is False

    # A fresh workflow must start cleanly afterwards.
    wired.window.timer.start(60)
    assert wired.window.timer.state is TimerState.RUNNING


# --- FIX 1 + FIX 2: user pause/resume controls the selected song; the
# --- countdown never consumes duration before playback is confirmed ---------


ZUNE_SONG = SessionInfo("ZuneMusic.exe", "Playing", title="Karma Police")
ZUNE_SONG_PAUSED = SessionInfo("ZuneMusic.exe", "Paused", title="Karma Police")


def _stub_local(monkeypatch, tmp_path) -> list:
    """Deterministic local workflow: a fake match, no real launch, no
    machine-registry player hint (so tests are machine-independent)."""
    import music_player.playback.local as local_module

    audio = tmp_path / "Radiohead - Karma Police.mp3"
    audio.write_bytes(b"x")
    match = local_search.MatchResult(path=audio, score=100.0, method="exact")
    monkeypatch.setattr(local_search, "search", lambda song: match)
    monkeypatch.setattr(local_module, "default_player_hint", lambda ext: None)
    started: list[str] = []
    monkeypatch.setattr(local_module.os, "startfile", lambda p: started.append(p))
    return started


def _start_via_ui(wired, song: str = "karma police", minutes: float = 2.0) -> None:
    wired.window.song_input.setText(song)
    wired.window.minutes_input.setValue(minutes)
    wired.window.start_workflow()


def test_start_holds_countdown_until_local_playback_confirmed(
    wired, qapp, clock, monkeypatch, tmp_path
):
    """FIX 2: Start holds the countdown (full duration) during search,
    launch, and loading; it begins with the FULL duration only once the
    selected song is confirmed Playing."""
    started = _stub_local(monkeypatch, tmp_path)
    _start_via_ui(wired, minutes=2.0)

    # Held from the moment Start was pressed — never RUNNING during discovery.
    assert wired.window.timer.state is TimerState.PAUSED
    clock.advance(30)  # slow discovery/loading: nothing may be consumed
    wired.window.timer.check()
    assert wired.window.timer.remaining_seconds() == 120.0

    wired.backend.sessions = [ZUNE_SONG, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)
    # Full duration at confirmation — the five-minute rule.
    assert wired.window.timer.remaining_seconds() == pytest.approx(120.0)
    assert started and "Karma Police" in started[0]

    clock.advance(10)
    wired.window.timer.check()
    assert wired.window.timer.remaining_seconds() == pytest.approx(110.0)


def test_local_wait_without_confirmed_playback_consumes_nothing(
    wired, qapp, clock, monkeypatch, tmp_path
):
    """FIX 2 (local): the player exposes no matching session → the countdown
    stays held with the full duration, honestly reported."""
    _stub_local(monkeypatch, tmp_path)
    _start_via_ui(wired, minutes=2.0)

    wired.backend.sessions = [SPOTIFY_OTHER]  # unrelated media only
    clock.advance(60)
    wired.window.timer.check()
    assert wired.window.timer.state is TimerState.PAUSED
    assert wired.window.timer.remaining_seconds() == 120.0


def test_user_pause_pauses_selected_local_session(
    wired, qapp, clock, monkeypatch, tmp_path
):
    """FIX 1: ⏸ pauses the countdown immediately with the exact remaining
    time and pauses the selected song's session — never an unrelated one."""
    _stub_local(monkeypatch, tmp_path)
    _start_via_ui(wired, minutes=2.0)
    wired.backend.sessions = [ZUNE_SONG, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)

    clock.advance(20)  # 100 s remain
    wired.window.timer.check()
    wired.window.pause_button.click()
    assert wired.window.timer.state is TimerState.PAUSED  # immediate
    assert wired.window.timer.remaining_seconds() == pytest.approx(100.0)  # exact

    assert _pump(qapp, lambda: wired.backend.pause_calls == [(ZUNE_SONG.source_id, "Karma Police")])
    assert wired.backend.pause_calls == [(ZUNE_SONG.source_id, "Karma Police")]  # Spotify untouched

    # The paused session is reported back — the countdown must stay paused.
    wired.backend.sessions = [ZUNE_SONG_PAUSED, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.media._last_status == "Paused")
    assert wired.window.timer.state is TimerState.PAUSED


def test_user_resume_continues_selected_song_and_countdown_after_confirmation(
    wired, qapp, clock, monkeypatch, tmp_path
):
    """FIX 1: ▶ resumes the selected song; the countdown resumes ONLY after
    the session is confirmed Playing — and from the exact remaining time."""
    _stub_local(monkeypatch, tmp_path)
    _start_via_ui(wired, minutes=2.0)
    wired.backend.sessions = [ZUNE_SONG, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)
    clock.advance(20)
    wired.window.timer.check()

    wired.window.pause_button.click()  # ⏸ → paused at 100 s
    wired.backend.sessions = [ZUNE_SONG_PAUSED, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.backend.pause_calls == [(ZUNE_SONG.source_id, "Karma Police")])

    wired.window.pause_button.click()  # ▶ → resume the song
    assert _pump(qapp, lambda: wired.backend.play_calls == [(ZUNE_SONG.source_id, "Karma Police")])
    # Command sent, playback NOT yet confirmed → countdown still paused.
    assert wired.window.timer.state is TimerState.PAUSED

    wired.backend.sessions = [ZUNE_SONG, SPOTIFY_OTHER]  # confirmed Playing
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)
    assert wired.window.timer.remaining_seconds() == pytest.approx(100.0)  # exact


def test_user_pause_with_no_song_touches_no_session(wired, qapp):
    """No song was started → honest report, and the unrelated playing
    session is never paused as a fallback."""
    wired.window.timer.start(120)  # countdown running without a workflow
    wired.backend.sessions = [SPOTIFY_OTHER]
    wired.window.pause_button.click()
    assert wired.window.timer.state is TimerState.PAUSED
    assert wired.backend.pause_calls == []
    assert "no song has been started" in wired.window.status_label.text().lower()


def test_youtube_user_pause_and_resume_control_selected_session(
    wired, qapp, clock, monkeypatch
):
    """FIX 1 (YouTube): ⏸/▶ control the selected song's Brave session while
    an unrelated Brave tab and another player keep playing untouched."""
    played: list = []
    _stub_youtube(monkeypatch, played)
    monkeypatch.setattr(local_search, "search", lambda song: None)
    _start_via_ui(wired, minutes=2.0)
    wired.backend.sessions = [BRAVE_SONG, BRAVE_OTHER, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)

    clock.advance(20)
    wired.window.timer.check()
    wired.window.pause_button.click()
    assert wired.window.timer.state is TimerState.PAUSED
    assert wired.window.timer.remaining_seconds() == pytest.approx(100.0)
    assert _pump(
        qapp, lambda: wired.backend.pause_calls == [(BRAVE_SONG.source_id, "Radiohead - Karma Police")]
    )

    wired.backend.sessions = [BRAVE_SONG_PAUSED, BRAVE_OTHER, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.media._last_status == "Paused")
    wired.window.pause_button.click()  # ▶
    assert _pump(
        qapp, lambda: wired.backend.play_calls == [(BRAVE_SONG.source_id, "Radiohead - Karma Police")]
    )
    assert wired.window.timer.state is TimerState.PAUSED  # not confirmed yet

    wired.backend.sessions = [BRAVE_SONG, BRAVE_OTHER, SPOTIFY_OTHER]
    assert _pump(qapp, lambda: wired.window.timer.state is TimerState.RUNNING)
    assert wired.window.timer.remaining_seconds() == pytest.approx(100.0)


def test_playback_failure_keeps_countdown_stopped_full_duration(
    wired, qapp, clock, monkeypatch, tmp_path
):
    """FIX 2: the launch fails → the countdown stays stopped with the full
    duration preserved, and the failure is reported honestly."""
    import music_player.playback.local as local_module

    started: list = []

    def failing_startfile(path: str) -> None:
        started.append(path)
        raise OSError("association broken")

    _stub_local(monkeypatch, tmp_path)
    monkeypatch.setattr(local_module.os, "startfile", failing_startfile)
    _start_via_ui(wired, minutes=2.0)

    assert _pump(qapp, lambda: started != [])  # search done, launch attempted
    clock.advance(30)
    wired.window.timer.check()
    assert wired.window.timer.state is TimerState.PAUSED  # stopped, not running
    assert wired.window.timer.remaining_seconds() == 120.0  # fully preserved
    assert wired.orch.current_source is None
    assert "could not open" in wired.window.status_label.text().lower()


def test_restart_clears_stale_source(wired, qapp, monkeypatch):
    """FIX 2: a new Start invalidates the previous workflow's source — a
    stale source must never be paused or controlled for the new song."""
    played: list = []
    _stub_youtube(monkeypatch, played)
    monkeypatch.setattr(local_search, "search", lambda song: None)
    wired.orch._source = "stale source from a previous workflow"
    wired.orch.start("a new song")
    assert wired.orch.current_source is None
    # Let the (stubbed) async workflow finish before teardown.
    assert _pump(qapp, lambda: played == ["a new song"])