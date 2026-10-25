"""Tests for playback sources and the orchestrator.

Mocked tests prove the *orchestration logic* only.  They are explicitly not
proof that real playback control works — real control depends on the
default Windows player and (phase 4) on the Windows media-session API.

Note: signals carrying Python objects (``Signal(object)``) are recorded with
plain Python callbacks rather than QSignalSpy, whose C++ side cannot marshal
PyObject payloads.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtTest import QSignalSpy

from music_player import local_search
from music_player.playback import (
    Capabilities,
    LocalFileSource,
    PlaybackError,
    PlaybackOrchestrator,
    PlaybackSource,
    PlaybackState,
)
from music_player.playback import local as local_module


def _pump(qapp, condition, timeout_ms: int = 2_000) -> bool:
    """Process events until *condition* is true (queued signals deliver)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while not condition():
        if time.monotonic() > deadline:
            return False
        qapp.processEvents()
        time.sleep(0.01)
    return True


@pytest.fixture
def source(tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"fake")
    return LocalFileSource(f)


# --- LocalFileSource: honest capabilities ------------------------------------------


def test_local_source_reports_no_control(source: LocalFileSource):
    caps = source.capabilities
    assert caps.can_pause is False
    assert caps.can_read_state is False
    assert caps.label == "Local file (default app)"
    assert source.state() is PlaybackState.UNKNOWN
    assert source.pause() is False


def test_local_source_play_calls_startfile(source: LocalFileSource, monkeypatch):
    launched = []
    monkeypatch.setattr(local_module.os, "startfile", lambda p: launched.append(p))
    source.play()
    assert launched == [str(source.path)]


def test_local_source_play_missing_file(tmp_path):
    with pytest.raises(PlaybackError):
        LocalFileSource(tmp_path / "gone.mp3").play()


def test_local_source_play_directory(tmp_path):
    with pytest.raises(PlaybackError):
        LocalFileSource(tmp_path).play()


def test_local_source_play_launch_failure(source: LocalFileSource, monkeypatch):
    def boom(_p):
        raise OSError("association broken")

    monkeypatch.setattr(local_module.os, "startfile", boom)
    with pytest.raises(PlaybackError, match="Could not open"):
        source.play()


# --- orchestrator: search → open / fallback ----------------------------------------


class FakeControllable(PlaybackSource):
    """Test double whose pause actually works."""

    def __init__(self) -> None:
        super().__init__(Capabilities(label="fake", can_pause=True, can_read_state=True))
        self.paused = False

    def play(self) -> None:
        pass

    def pause(self) -> bool:
        self.paused = True
        return True

    def state(self) -> PlaybackState:
        return PlaybackState.PAUSED if self.paused else PlaybackState.PLAYING


class Recorder:
    """Plain-Python signal recorder (safe for object payloads)."""

    def __init__(self, orch: PlaybackOrchestrator) -> None:
        self.statuses: list[tuple[str, bool]] = []
        self.sources: list[object] = []
        orch.status.connect(lambda msg, err: self.statuses.append((msg, err)))
        orch.source_started.connect(self.sources.append)


@pytest.fixture
def orch(qapp) -> PlaybackOrchestrator:
    return PlaybackOrchestrator()


def test_start_empty_song_is_rejected(orch: PlaybackOrchestrator):
    rec = Recorder(orch)
    orch.start("   ")
    assert len(rec.statuses) == 1
    assert "Enter a song" in rec.statuses[0][0]
    assert rec.sources == []  # nothing launched


def test_start_opens_local_match(orch, qapp, tmp_path, monkeypatch):
    audio = tmp_path / "Karma Police.mp3"
    audio.write_bytes(b"fake")
    monkeypatch.setattr(local_search, "search", lambda q: local_search.MatchResult(audio, 100, "exact"))
    launched = []
    monkeypatch.setattr(local_module.os, "startfile", lambda p: launched.append(p))

    rec = Recorder(orch)
    orch.start("Karma Police")

    assert _pump(qapp, lambda: bool(rec.sources)), "search result never arrived"
    opened = rec.sources[0]
    assert isinstance(opened, LocalFileSource)
    assert launched == [str(audio)]
    assert "Playing" in rec.statuses[-1][0]
    assert orch.current_source is opened


def test_no_match_without_fallback_reports_honestly(orch, qapp, monkeypatch):
    monkeypatch.setattr(local_search, "search", lambda q: None)
    rec = Recorder(orch)
    orch.start("obscure song")
    assert _pump(qapp, lambda: "No local match" in rec.statuses[-1][0] if rec.statuses else False)
    assert "No local match" in rec.statuses[-1][0]


def test_no_match_invokes_youtube_fallback(orch, qapp, monkeypatch):
    monkeypatch.setattr(local_search, "search", lambda q: None)
    calls = []
    orch.set_youtube_fallback(lambda song: calls.append(song))
    Recorder(orch)
    orch.start("obscure song")
    assert _pump(qapp, lambda: bool(calls)), "fallback never invoked"
    assert calls == ["obscure song"]


def test_failing_youtube_fallback_is_reported(orch, qapp, monkeypatch):
    """The fallback runs on the pool thread (network I/O); its failure must
    still reach the user as a status message, never as a crash."""
    monkeypatch.setattr(local_search, "search", lambda q: None)

    def boom(_song):
        raise RuntimeError("network down")

    orch.set_youtube_fallback(boom)
    rec = Recorder(orch)
    orch.start("obscure song")
    assert _pump(qapp, lambda: any("YouTube fallback failed" in m for m, _e in rec.statuses))
    assert rec.sources == []


def test_search_error_is_reported_not_raised(orch, qapp, monkeypatch):
    def boom(_q):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(local_search, "search", boom)
    rec = Recorder(orch)
    orch.start("anything")
    assert _pump(qapp, lambda: any("Local search failed" in m for m, _e in rec.statuses))
    assert "Local search failed" in rec.statuses[-1][0]
    assert rec.sources == []


def test_stale_search_results_are_dropped(orch):
    rec = Recorder(orch)
    # A result for an abandoned request (older sequence) must be ignored.
    orch._sequence = 5
    orch._on_search_done(sequence=4, match=None, error="")
    assert rec.statuses == []


def test_failed_open_reports_error(orch, qapp, tmp_path, monkeypatch):
    # Match points at a file that vanishes between search and open.
    ghost = tmp_path / "ghost.mp3"
    monkeypatch.setattr(local_search, "search", lambda q: local_search.MatchResult(ghost, 100, "exact"))
    rec = Recorder(orch)
    orch.start("ghost song")
    assert _pump(qapp, lambda: bool(rec.statuses) and "File not found" in rec.statuses[-1][0])
    assert rec.sources == []


# --- pause -----------------------------------------------------------------------


def test_pause_nothing_started(orch: PlaybackOrchestrator):
    assert orch.pause_playback() == (None, False)


def test_pause_uncontrollable_source(orch, qapp, tmp_path, monkeypatch):
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"fake")
    monkeypatch.setattr(local_search, "search", lambda q: local_search.MatchResult(audio, 100, "exact"))
    monkeypatch.setattr(local_module.os, "startfile", lambda p: None)
    rec = Recorder(orch)
    orch.start("song")
    assert _pump(qapp, lambda: orch.current_source is not None)
    source, paused = orch.pause_playback()
    assert isinstance(source, LocalFileSource)
    assert paused is False  # honest: default app cannot be paused directly


def test_pause_controllable_source(orch):
    fake = FakeControllable()
    orch.report_source(fake)
    source, paused = orch.pause_playback()
    assert source is fake
    assert paused is True
    assert fake.state() is PlaybackState.PAUSED


def test_report_source_emits_signal(orch):
    fake = FakeControllable()
    rec = Recorder(orch)
    orch.report_source(fake)
    assert rec.sources == [fake]
    assert orch.current_source is fake


def test_reset_forgets_source(orch):
    orch.report_source(FakeControllable())
    orch.reset()
    assert orch.current_source is None
    assert orch.pause_playback() == (None, False)


def test_status_signal_carries_error_flag(orch):
    rec = Recorder(orch)
    orch.status.emit("plain", False)
    orch.status.emit("bad", True)
    assert rec.statuses == [("plain", False), ("bad", True)]