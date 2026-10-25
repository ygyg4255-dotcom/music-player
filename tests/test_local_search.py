"""Tests for local Music-folder search and fuzzy song matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from music_player import config, local_search
from music_player.local_search import MatchResult, find_song, music_dir, scan_audio


@pytest.fixture
def music_tree(tmp_path: Path) -> Path:
    """A small Music folder with audio files, junk files, and a subfolder."""
    (tmp_path / "Karma Police.mp3").write_bytes(b"fake")
    (tmp_path / "Radiohead - Karma Police.mp3").write_bytes(b"fake")
    (tmp_path / "Creep.wav").write_bytes(b"fake")
    (tmp_path / "notes.txt").write_text("not audio")
    sub = tmp_path / "albums"
    sub.mkdir()
    (sub / "OK Computer - 04 No Surprises.flac").write_bytes(b"fake")
    return tmp_path


# --- scan ------------------------------------------------------------------


def test_scan_collects_supported_audio_recursively(music_tree: Path):
    files = scan_audio(music_tree)
    names = {f.name for f in files}
    assert names == {
        "Karma Police.mp3",
        "Radiohead - Karma Police.mp3",
        "Creep.wav",
        "OK Computer - 04 No Surprises.flac",
    }


def test_scan_ignores_unsupported_formats(music_tree: Path):
    files = scan_audio(music_tree)
    assert all(f.suffix.lower() in config.SUPPORTED_EXTENSIONS for f in files)
    assert not any(f.name == "notes.txt" for f in files)


def test_scan_missing_folder_is_empty(tmp_path: Path):
    assert scan_audio(tmp_path / "nope") == []


def test_scan_is_bounded(music_tree: Path, monkeypatch):
    monkeypatch.setattr(local_search, "MAX_FILES_PER_SCAN", 2)
    assert len(scan_audio(music_tree)) == 2


# --- matching ----------------------------------------------------------------


def test_exact_match_wins(music_tree: Path):
    result = find_song("Karma Police", scan_audio(music_tree))
    assert result is not None
    assert result.method == "exact"
    assert result.score == 100
    assert result.path.name == "Karma Police.mp3"


def test_exact_match_case_and_punctuation_insensitive(music_tree: Path):
    result = find_song("karma   police", scan_audio(music_tree))
    assert result is not None
    assert result.method == "exact"


def test_fuzzy_match_prefers_closer_title(music_tree: Path):
    # "Karma Police.mp3" is an exact hit, so use a query that only fuzzy
    # matches: the fuller title must beat a weaker candidate.
    files = [music_tree / "Radiohead - Karma Police.mp3", music_tree / "Creep.wav"]
    result = find_song("karma police", files)
    assert result is not None
    assert result.method == "fuzzy"
    assert result.path.name == "Radiohead - Karma Police.mp3"
    assert result.score >= config.MATCH_CLOSE_THRESHOLD


def test_fuzzy_match_reaches_into_subfolders(music_tree: Path):
    result = find_song("no surprises", scan_audio(music_tree))
    assert result is not None
    assert result.path.parent.name == "albums"


def test_weak_match_rejected(music_tree: Path):
    # Nothing remotely resembles this query → YouTube fallback territory.
    assert find_song("zzz qqq unreleased b-side", scan_audio(music_tree)) is None


def test_no_files_yields_none(music_tree: Path):
    assert find_song("anything", []) is None


def test_empty_query_yields_none(music_tree: Path):
    assert find_song("   ", scan_audio(music_tree)) is None


# --- music folder resolution ----------------------------------------------------


def test_music_dir_resolves_or_is_none():
    """On this Windows machine the Music folder must resolve; smoke check."""
    folder = music_dir()
    assert folder is None or folder.is_dir()


def test_music_dir_falls_back_to_profile_music(tmp_path: Path, monkeypatch):
    (tmp_path / "Music").mkdir()
    monkeypatch.setattr(local_search, "_known_folder_music", lambda: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert music_dir() == tmp_path / "Music"


def test_music_dir_none_when_unresolvable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(local_search, "_known_folder_music", lambda: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no Music subdir
    assert music_dir() is None


# --- end-to-end search helper ------------------------------------------------------


def test_search_full_workflow(music_tree: Path, monkeypatch):
    monkeypatch.setattr(local_search, "_known_folder_music", lambda: music_tree)
    result = local_search.search("karma police")
    assert isinstance(result, MatchResult)
    assert result.score >= config.MATCH_MIN_THRESHOLD


def test_search_returns_none_without_music_folder(monkeypatch):
    monkeypatch.setattr(local_search, "_known_folder_music", lambda: None)
    monkeypatch.setattr(Path, "home", lambda: Path("Z:/definitely/not/here"))
    assert local_search.search("karma police") is None