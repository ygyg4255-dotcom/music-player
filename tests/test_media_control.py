"""Tests for Windows media-session control (GSMTC layer).

Unit tests use an in-process fake backend: they prove the decision logic
and signal plumbing, not real media control.  Real pause/resume was
verified separately against PotPlayer via
``scripts/probe_media_control_live.py`` (PASS on this machine).
"""

from __future__ import annotations

import time

import pytest

from music_player.media_control import (
    MediaController,
    SessionInfo,
    pick_session,
)

POT = SessionInfo("PotPlayerMini64.exe", "Playing")
POT_PAUSED = SessionInfo("PotPlayerMini64.exe", "Paused")
BRAVE = SessionInfo("BraveBrowser.XYZ", "Playing")
SPOTIFY_PAUSED = SessionInfo("Spotify.exe", "Paused")


def _pump(qapp, condition, timeout_ms: int = 3_000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while not condition():
        if time.monotonic() > deadline:
            return False
        qapp.processEvents()
        time.sleep(0.01)
    return True


# --- session picking (pure decision logic) -----------------------------------


def test_pick_by_tracked_identity():
    assert pick_session([BRAVE, POT], None, tracked_id=POT.source_id) == POT


def test_pick_tracked_id_gone_returns_none():
    assert pick_session([BRAVE], None, tracked_id=POT.source_id) is None


def test_pick_hint_narrows_candidates():
    assert pick_session([POT, BRAVE], "brave") == BRAVE


def test_pick_unique_playing_session():
    assert pick_session([POT, SPOTIFY_PAUSED], None) == POT


def test_pick_multiple_playing_is_ambiguous():
    assert pick_session([POT, BRAVE], None) is None


def test_pick_single_candidate_even_if_not_playing():
    assert pick_session([SPOTIFY_PAUSED], None) == SPOTIFY_PAUSED


def test_pick_no_sessions():
    assert pick_session([], None) is None


def test_pick_multiple_paused_without_hint_refuses():
    assert pick_session([POT_PAUSED, SPOTIFY_PAUSED], None) is None


def test_pick_prefers_playing_among_hinted():
    brave_paused = SessionInfo("BraveBrowser.XYZ", "Paused")
    assert pick_session([brave_paused, BRAVE], "brave") == BRAVE


# --- session picking with a title query (YouTube fallback) ----------------------

SONG = "karma police"
BRAVE_SONG_PAUSED = SessionInfo("BraveBrowser.XYZ", "Paused", title="Radiohead - Karma Police")
BRAVE_SONG = SessionInfo("BraveBrowser.XYZ", "Playing", title="Radiohead - Karma Police")
BRAVE_OTHER = SessionInfo("BraveBrowser.XYZ", "Playing", title="Some Other Video")
POT_OTHER = SessionInfo("PotPlayerMini64.exe", "Playing", title="Another Song")


def test_title_query_picks_unique_match_even_if_paused():
    # The song's tab exists but has not started yet: it must be tracked so
    # its Playing report can start the countdown.
    assert pick_session([BRAVE_SONG_PAUSED, POT_OTHER], "brave", title_query=SONG) == BRAVE_SONG_PAUSED


def test_title_query_never_picks_unrelated_playing_session():
    # Another Brave tab plays something else: never track or control it.
    assert pick_session([BRAVE_OTHER], "brave", title_query=SONG) is None


def test_title_query_prefers_playing_among_multiple_matches():
    duplicate = SessionInfo("BraveBrowser.DUP", "Paused", title="Karma Police (Remastered)")
    assert pick_session([duplicate, BRAVE_SONG], "brave", title_query=SONG) == BRAVE_SONG


def test_title_query_multiple_matches_none_playing_refuses():
    duplicate = SessionInfo("BraveBrowser.DUP", "Paused", title="Karma Police (Remastered)")
    assert pick_session([duplicate, BRAVE_SONG_PAUSED], "brave", title_query=SONG) is None


def test_title_query_ignores_hintless_unrelated_sessions():
    # The matching session may even be outside the hinted app set only when
    # the hint has no candidates at all — here Brave exists and wins.
    assert pick_session([BRAVE_SONG, POT_OTHER], None, title_query=SONG) == BRAVE_SONG


def test_title_match_normalizes_case_and_punctuation():
    brave = SessionInfo("BraveBrowser.XYZ", "Playing", title="radiohead: karma   police!")
    assert pick_session([brave], "brave", title_query="Karma Police") == brave


# --- controller with fake backend ---------------------------------------------


class FakeBackend:
    """Async stand-in for the winrt backend; the test mutates `sessions`."""

    def __init__(self) -> None:
        self.sessions: list[SessionInfo] = []
        self.pause_calls: list[str] = []
        self.play_calls: list[str] = []

    async def get_sessions(self) -> list[SessionInfo]:
        return list(self.sessions)

    async def pause(self, source_id: str, title: str = "") -> bool:
        self.pause_calls.append((source_id, title))
        return True

    async def play(self, source_id: str, title: str = "") -> bool:
        self.play_calls.append((source_id, title))
        return True


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def controller(qapp, backend: FakeBackend):
    ctrl = MediaController(backend_factory=lambda: backend, poll_interval_s=0.02)
    yield ctrl
    ctrl._shutdown()


def _record(ctrl: MediaController):
    events = {"tracked": [], "lost": [], "pause": [], "control": []}
    ctrl.tracked_state.connect(lambda s, st: events["tracked"].append((s, st)))
    ctrl.session_lost.connect(lambda s: events["lost"].append(s))
    ctrl.pause_result.connect(lambda s, ok, m: events["pause"].append((s, ok, m)))
    ctrl.control_result.connect(lambda s, ok, m: events["control"].append((s, ok, m)))
    return events


def test_monitoring_reports_status_changes(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [POT]
    controller.start_monitoring("potplayer")

    assert _pump(qapp, lambda: any(s == "Playing" for _s, s in events["tracked"]))
    backend.sessions = [POT_PAUSED]
    assert _pump(qapp, lambda: any(s == "Paused" for _s, s in events["tracked"]))


def test_monitoring_reports_session_loss(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [POT]
    controller.start_monitoring("potplayer")
    assert _pump(qapp, lambda: bool(events["tracked"]))

    backend.sessions = []  # player closed
    assert _pump(qapp, lambda: bool(events["lost"]))
    assert events["lost"] == [POT.source_id]


def test_stop_monitoring_stops_polling(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [POT]
    controller.start_monitoring("potplayer")
    assert _pump(qapp, lambda: bool(events["tracked"]))

    controller.stop_monitoring()
    count_before = len(events["tracked"])
    backend.sessions = [POT_PAUSED]
    time.sleep(0.15)
    qapp.processEvents()
    assert len(events["tracked"]) == count_before  # nothing further reported


def test_pause_relevant_pauses_unique_playing(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [POT, SPOTIFY_PAUSED]
    controller.pause_relevant(None)
    assert _pump(qapp, lambda: bool(events["pause"]))
    source_id, ok, message = events["pause"][0]
    assert source_id == POT.source_id
    assert ok is True
    assert backend.pause_calls == [(POT.source_id, "")]


def test_pause_relevant_uses_hint(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [POT, BRAVE]  # two playing sessions…
    controller.pause_relevant("brave")  # …but the hint disambiguates
    assert _pump(qapp, lambda: bool(events["pause"]))
    assert backend.pause_calls == [(BRAVE.source_id, "")]


def test_pause_relevant_refuses_ambiguity(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [POT, BRAVE]
    controller.pause_relevant(None)
    assert _pump(qapp, lambda: bool(events["pause"]))
    _s, ok, message = events["pause"][0]
    assert ok is False
    assert "unambiguous" in message


def test_pause_relevant_reports_backend_failure(controller, backend, qapp):
    async def failing_pause(source_id, title=""):
        return False

    backend.pause = failing_pause  # type: ignore[method-assign]
    events = _record(controller)
    backend.sessions = [POT]
    controller.pause_relevant(None)
    assert _pump(qapp, lambda: bool(events["pause"]))
    _s, ok, message = events["pause"][0]
    assert ok is False
    assert "refused" in message


def test_monitoring_with_title_tracks_only_matching_session(controller, backend, qapp):
    events = _record(controller)
    # Another Brave tab plays an unrelated video: it must not be tracked.
    backend.sessions = [BRAVE_OTHER]
    controller.start_monitoring("brave", title_query="karma police")
    time.sleep(0.15)
    qapp.processEvents()
    assert events["tracked"] == []  # unrelated media never tracked

    # The selected song's tab appears and plays → tracked, status reported.
    backend.sessions = [BRAVE_OTHER, BRAVE_SONG]
    assert _pump(qapp, lambda: any(st == "Playing" for _s, st in events["tracked"]))
    assert all(s == BRAVE_SONG.source_id for s, _st in events["tracked"])


def test_pause_relevant_uses_title_query(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [BRAVE_SONG, BRAVE_OTHER]  # two playing Brave sessions…
    controller.pause_relevant("brave", title_query="karma police")  # …title decides
    assert _pump(qapp, lambda: bool(events["pause"]))
    assert backend.pause_calls == [(BRAVE_SONG.source_id, "Radiohead - Karma Police")]


def test_pause_relevant_reports_missing_title_match(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [BRAVE_OTHER]  # exists, but plays a different song
    controller.pause_relevant("brave", title_query="karma police")
    assert _pump(qapp, lambda: bool(events["pause"]))
    _s, ok, message = events["pause"][0]
    assert ok is False
    assert "karma police" in message
    assert backend.pause_calls == []  # unrelated session untouched


def test_recheck_reemits_current_status(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [POT]
    controller.start_monitoring("potplayer")
    assert _pump(qapp, lambda: len(events["tracked"]) == 1)

    controller.recheck()  # e.g. the countdown was just gated on playback
    assert _pump(qapp, lambda: len(events["tracked"]) == 2)
    assert events["tracked"][1] == (POT.source_id, "Playing")


def test_unavailable_controller_reports_honestly(qapp, monkeypatch):
    import music_player.media_control as mc

    monkeypatch.setattr(mc, "_winrt_available", lambda: False)
    ctrl = MediaController()
    events = _record(ctrl)
    ctrl.pause_relevant(None)
    # No worker thread involved: the honest refusal is emitted immediately.
    qapp.processEvents()
    assert events["pause"] and events["pause"][0][1] is False
    assert "unavailable" in events["pause"][0][2]
    ctrl.start_monitoring("x")  # must be a quiet no-op, never raise
    ctrl._shutdown()


def test_monitoring_unavailable_is_quiet(qapp, monkeypatch):
    import music_player.media_control as mc

    monkeypatch.setattr(mc, "_winrt_available", lambda: False)
    ctrl = MediaController()
    ctrl.start_monitoring("hint")  # must not spawn threads or raise
    assert ctrl._thread is None
    ctrl._shutdown()


# --- user pause/resume of the *selected* session (FIX 1) -----------------------


def test_pause_selected_uses_tracked_identity(controller, backend, qapp):
    """The monitored session is paused by identity — never another session,
    even with several unrelated ones playing."""
    events = _record(controller)
    backend.sessions = [BRAVE_SONG, SPOTIFY_PAUSED]
    controller.start_monitoring("brave", title_query="karma police")
    assert _pump(qapp, lambda: controller._tracked_id == BRAVE_SONG.source_id)

    controller.pause_selected()
    assert _pump(qapp, lambda: bool(events["control"]))
    assert backend.pause_calls == [(BRAVE_SONG.source_id, "Radiohead - Karma Police")]
    assert events["control"][0][1] is True


def test_resume_selected_plays_tracked_session(controller, backend, qapp):
    events = _record(controller)
    backend.sessions = [BRAVE_SONG_PAUSED]
    controller.start_monitoring("brave", title_query="karma police")
    assert _pump(qapp, lambda: controller._tracked_id == BRAVE_SONG.source_id)

    controller.resume_selected()
    assert _pump(qapp, lambda: bool(events["control"]))
    assert backend.play_calls == [(BRAVE_SONG_PAUSED.source_id, "Radiohead - Karma Police")]
    assert backend.pause_calls == []  # resume never pauses anything


def test_pause_selected_without_session_reports_honestly(controller, backend, qapp):
    """No session for the selected song → honest failure, nothing paused."""
    events = _record(controller)
    backend.sessions = [BRAVE_OTHER, SPOTIFY_PAUSED]  # unrelated only
    controller.pause_selected("brave", title_query="karma police")
    assert _pump(qapp, lambda: bool(events["control"]))
    _s, ok, message = events["control"][0]
    assert ok is False
    assert "no controllable media session" in message
    assert backend.pause_calls == []  # an unrelated session is never a fallback


def test_pause_selected_reports_backend_refusal(controller, backend, qapp):
    async def refusing_pause(source_id, title=""):
        return False

    backend.pause = refusing_pause  # type: ignore[method-assign]
    events = _record(controller)
    backend.sessions = [BRAVE_SONG]
    controller.start_monitoring("brave", title_query="karma police")
    assert _pump(qapp, lambda: controller._tracked_id == BRAVE_SONG.source_id)

    controller.pause_selected()
    assert _pump(qapp, lambda: bool(events["control"]))
    _s, ok, message = events["control"][0]
    assert ok is False
    assert "refused" in message


def test_selected_control_unavailable_reports_honestly(qapp, monkeypatch):
    import music_player.media_control as mc

    monkeypatch.setattr(mc, "_winrt_available", lambda: False)
    ctrl = MediaController()
    events = _record(ctrl)
    ctrl.pause_selected("brave", title_query="karma police")
    qapp.processEvents()
    assert events["control"][0][1] is False
    assert "only the countdown was paused" in events["control"][0][2]
    ctrl.resume_selected("brave", title_query="karma police")
    qapp.processEvents()
    assert "countdown stays paused" in events["control"][1][2]
    ctrl._shutdown()


# --- a tracked app can reuse its session for unrelated media -------------------
# (observed live: Brave switched its single "Brave" session from the selected
# song's tab to an unrelated tab's video while the countdown depended on it)


def test_tracked_id_with_switched_title_is_not_the_song():
    # Same id as the tracked session, but a different media title → the
    # identity match must NOT win over the title.
    assert (
        pick_session(
            [BRAVE_OTHER], "brave", tracked_id=BRAVE_OTHER.source_id, title_query=SONG
        )
        is None
    )


def test_tracked_id_switch_falls_through_to_title_match():
    # Two sessions share the id (one per tab): after the tracked one switched
    # to other media, the title match on the other session must win.
    assert (
        pick_session(
            [BRAVE_OTHER, BRAVE_SONG],
            "brave",
            tracked_id=BRAVE_OTHER.source_id,
            title_query=SONG,
        )
        == BRAVE_SONG
    )


def test_tracked_id_with_empty_title_is_trusted():
    # Properties unavailable (empty title) → identity is trusted as before.
    opaque = SessionInfo("BraveBrowser.XYZ", "Playing", title="")
    assert pick_session([opaque], "brave", tracked_id=opaque.source_id, title_query=SONG) == opaque


def test_tracked_id_gone_with_title_query_is_still_none():
    # A vanished tracked id must not resurrect via a fresh title-less pick.
    assert pick_session([SPOTIFY_PAUSED], "brave", tracked_id=BRAVE.source_id, title_query=SONG) is None


def test_monitoring_loses_session_when_app_switches_media(controller, backend, qapp):
    """The app reuses its single session for another video → session_lost,
    never tracking the unrelated media as if it were the selected song."""
    events = _record(controller)
    backend.sessions = [BRAVE_SONG]
    controller.start_monitoring("brave", title_query="karma police")
    assert _pump(qapp, lambda: len(events["tracked"]) == 1)  # Playing delivered

    backend.sessions = [BRAVE_OTHER]  # same id, unrelated title (tab switch)
    tracked_before_switch = len(events["tracked"])
    assert _pump(qapp, lambda: bool(events["lost"]))
    assert controller._tracked_id is None
    time.sleep(0.1)
    qapp.processEvents()
    # No further status reports: the unrelated media is not tracked.
    assert len(events["tracked"]) == tracked_before_switch


def test_pause_selected_with_switched_session_touches_nothing(controller, backend, qapp):
    """⏸ after the app switched its session to unrelated media → honest
    refusal; the unrelated session is never paused as a fallback."""
    events = _record(controller)
    controller._tracked_id = BRAVE_SONG.source_id  # was tracked before the switch
    backend.sessions = [BRAVE_OTHER]  # same id, different title now

    controller.pause_selected("brave", title_query="karma police")
    assert _pump(qapp, lambda: bool(events["control"]))
    _s, ok, message = events["control"][0]
    assert ok is False
    assert backend.pause_calls == []  # the unrelated tab was not touched