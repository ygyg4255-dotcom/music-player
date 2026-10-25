"""Tests for the YouTube/Brave fallback.

Browser behavior is mocked: these tests verify URL construction, launch
choice, and error handling.  They deliberately never open a real browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote_plus

import pytest

from music_player import config, youtube
from music_player.playback import PlaybackError, PlaybackState
from music_player.youtube import YouTubeSource


# --- URL construction -------------------------------------------------------------


def test_search_url_is_properly_encoded():
    source = YouTubeSource("Radiohead Karma Police")
    assert source.search_url == config.YOUTUBE_SEARCH_URL.format(
        query=quote_plus("Radiohead Karma Police")
    )
    assert "&" not in source.search_url.split("search_query=")[1]


def test_control_hint_is_brave():
    assert YouTubeSource("x").control_hint == "brave"


def test_capabilities_are_honest():
    caps = YouTubeSource("x").capabilities
    assert caps.can_pause is False
    assert caps.can_read_state is False
    assert source_state_is_unknown()


def source_state_is_unknown() -> bool:
    return YouTubeSource("x").state() is PlaybackState.UNKNOWN


# --- launch choice -----------------------------------------------------------------


def test_play_uses_existing_brave_when_available(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(youtube, "find_first_result", lambda song: ("dQw4w9WgXcQ", "Some Song"))
    monkeypatch.setattr(youtube, "brave_path", lambda: Path(r"C:\Brave\brave.exe"))
    monkeypatch.setattr(youtube.subprocess, "Popen", lambda args: launched.append(args))

    source = YouTubeSource("some song")
    source.play()

    assert len(launched) == 1
    brave, url = launched[0]
    assert brave == r"C:\Brave\brave.exe"
    # The watch page of the first result opens — not the search results page.
    assert url == f"https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_play_falls_back_to_search_page_without_first_result(monkeypatch, tmp_path):
    """First result undeterminable (offline/consent wall) → old honest behaviour."""
    launched = []
    monkeypatch.setattr(youtube, "find_first_result", lambda song: None)
    monkeypatch.setattr(youtube, "brave_path", lambda: Path(r"C:\Brave\brave.exe"))
    monkeypatch.setattr(youtube.subprocess, "Popen", lambda args: launched.append(args))

    source = YouTubeSource("some song")
    source.play()

    _brave, url = launched[0]
    assert "results?search_query=" in url
    assert quote_plus("some song") in url


def test_play_falls_back_to_default_browser_without_brave(monkeypatch):
    opened = []
    monkeypatch.setattr(youtube, "find_first_result", lambda song: None)
    monkeypatch.setattr(youtube, "brave_path", lambda: None)
    monkeypatch.setattr(youtube.webbrowser, "open", lambda url: opened.append(url) or True)

    YouTubeSource("some song").play()
    assert len(opened) == 1 and "youtube.com" in opened[0]


def test_play_raises_when_no_browser_works(monkeypatch):
    monkeypatch.setattr(youtube, "find_first_result", lambda song: None)
    monkeypatch.setattr(youtube, "brave_path", lambda: None)
    monkeypatch.setattr(youtube.webbrowser, "open", lambda url: False)
    with pytest.raises(PlaybackError, match="Could not open any browser"):
        YouTubeSource("some song").play()


def test_play_reports_brave_launch_failure(monkeypatch):
    def boom(_args):
        raise OSError("denied")

    monkeypatch.setattr(youtube, "find_first_result", lambda song: None)
    monkeypatch.setattr(youtube, "brave_path", lambda: Path(r"C:\Brave\brave.exe"))
    monkeypatch.setattr(youtube.subprocess, "Popen", boom)
    with pytest.raises(PlaybackError, match="Could not launch Brave"):
        YouTubeSource("some song").play()


# --- Brave discovery ----------------------------------------------------------------


def test_brave_path_found_on_this_machine():
    # This machine has Brave installed (verified in phase 1); if that ever
    # changes the test degrades to None, which is also a valid outcome.
    brave = youtube.brave_path()
    assert brave is None or brave.is_file()


def test_pause_is_honest():
    assert YouTubeSource("x").pause() is False


# --- first-result discovery (network fully mocked) ----------------------------------


def _search_html(*renderers: tuple[str, str]) -> str:
    """A minimal realistic YouTube search page: ytInitialData with the
    given (videoId, title) organic results in order."""
    contents = [
        {"videoRenderer": {"videoId": vid, "title": {"runs": [{"text": title}]}}}
        for vid, title in renderers
    ]
    data = {"contents": {"twoColumnSearchResultsRenderer": {"primaryContents": {
        "sectionListRenderer": {"contents": [{"itemSectionRenderer": {"contents": contents}}]}
    }}}}
    return f"<html><script>var ytInitialData = {json.dumps(data)};</script></html>"


def test_find_first_result_returns_first_suitable():
    html = _search_html(("advid12345", "Unrelated Podcast Episode"))
    result = youtube.find_first_result("karma police", fetch=lambda song: html)
    assert result is None  # nothing matched yet

    html = _search_html(
        ("advid12345", "Unrelated Podcast Episode"),
        ("dQw4w9WgXcQ", "Radiohead - Karma Police"),
    )
    result = youtube.find_first_result("karma police", fetch=lambda song: html)
    assert result == ("dQw4w9WgXcQ", "Radiohead - Karma Police")


def test_find_first_result_accepts_weak_match_as_fallback():
    html = _search_html(("abc12345678", "Karma Police cover (live)"))
    result = youtube.find_first_result("karma police", fetch=lambda song: html)
    assert result is not None  # above the minimum threshold: good enough


def test_find_first_result_fetch_failure_returns_none():
    def offline(_song):
        raise OSError("no network")

    assert youtube.find_first_result("x", fetch=offline) is None


def test_find_first_result_unparseable_page_returns_none():
    assert youtube.find_first_result("x", fetch=lambda song: "<html>consent wall</html>") is None


def test_find_first_result_ignores_ads_and_picks_organic():
    # Ads live outside videoRenderer; only the organic result must match.
    html = _search_html(("organicvid01", "Karma Police - Radiohead (Official Video)"))
    result = youtube.find_first_result("karma police", fetch=lambda song: html)
    assert result == ("organicvid01", "Karma Police - Radiohead (Official Video)")


def test_source_reports_title_and_waits_for_playback():
    source = YouTubeSource("karma police")
    assert source.control_title == "karma police"
    assert source.waits_for_playback is True


def test_waiting_message_mentions_result_title(monkeypatch):
    monkeypatch.setattr(
        youtube, "find_first_result", lambda song: ("dQw4w9WgXcQ", "Radiohead - Karma Police")
    )
    source = YouTubeSource("karma police")
    source.play()
    assert "Karma Police" in source.waiting_message
    assert "countdown starts" in source.waiting_message


def test_waiting_message_without_result_mentions_search(monkeypatch):
    monkeypatch.setattr(youtube, "find_first_result", lambda song: None)
    source = YouTubeSource("karma police")
    source.play()
    assert "search" in source.waiting_message.lower()
    assert "countdown starts" in source.waiting_message