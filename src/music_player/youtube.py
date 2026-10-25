"""YouTube fallback: play the first result in the user's *existing* Brave.

Integration reality (inspected, not assumed):

* Launching ``brave.exe <url>`` targets the already-running browser process
  when one exists (Chromium single-instance behaviour per profile), so the
  page opens as a new tab in the user's existing session.  If Brave is not
  running, it starts normally.  No profile is touched, no flags are added,
  and the browser is never closed by the music player.
* The *first suitable result* is determined **without any browser
  automation**: the app fetches the YouTube search page itself, parses the
  embedded ``ytInitialData`` JSON, and opens the watch URL of the first
  result whose title matches the requested song.  No remote debugging
  (CDP), no key/click synthesis, no Brave settings or profile changes —
  the browser is only ever handed a URL.
* Whether that watch page actually *starts playing* is decided by the
  browser (autoplay policy / Media Engagement Index) and YouTube, not by
  this app.  The source therefore reports ``waits_for_playback`` so the
  application layer holds the countdown until the matching Windows media
  session is observed ``Playing``; if playback never starts, the user's
  timer duration is not consumed and the status line says so honestly.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from . import config
from .playback.base import Capabilities, PlaybackError, PlaybackSource, PlaybackState

logger = logging.getLogger(__name__)

_BRAVE_RELATIVE = Path("BraveSoftware") / "Brave-Browser" / "Application" / "brave.exe"

#: Fetching the search page is bounded so a slow network can never hang the
#: workflow thread; on failure the search page opens instead (old behaviour).
_FETCH_TIMEOUT_S = 8.0
#: A plain browser identity; YouTube's consent/anti-bot walls depend on it.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _normalize(text: str) -> str:
    """Casefold + strip diacritics + collapse whitespace (same idea as the
    local-search normalizer, kept local so the modules stay decoupled)."""
    return " ".join(default_process(text).split())


def brave_path() -> Path | None:
    """Locate the installed Brave executable, or None."""
    candidates: list[Path] = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / _BRAVE_RELATIVE)
    # Registry App Paths knows the real location even for custom installs.
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe",
        ) as key:
            located, _ = winreg.QueryValueEx(key, None)
            candidates.insert(0, Path(located))
    except OSError:
        pass  # not registered — fine, the filesystem candidates still apply
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# --- first-result discovery (pure parsing; injectable fetch for tests) --------


def _fetch_search_html(song: str) -> str:
    """Fetch the YouTube search results page HTML (no browser involved)."""
    url = config.YOUTUBE_SEARCH_URL.format(query=quote_plus(song))
    request = Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )
    with urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
        return response.read().decode("utf-8", errors="replace")


def _yt_initial_data(html: str):
    """Extract the ytInitialData JSON object embedded in the search page."""
    marker = "var ytInitialData = "
    start = html.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = html.find("</script>", start)
    if end < 0:
        return None
    try:
        return json.loads(html[start:end].strip().rstrip(";"))
    except json.JSONDecodeError:
        return None


def _video_renderers(node, out: list[tuple[str, str]], limit: int = 12) -> None:
    """Collect ``(video_id, title)`` pairs in search-result order.

    Walks the ytInitialData tree recursively; only ``videoRenderer`` entries
    (organic results) are collected, so ads/shelf/shorts renderers cannot
    be mistaken for the first result.
    """
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict) and renderer.get("videoId"):
            title = ""
            runs = renderer.get("title", {}).get("runs")
            if isinstance(runs, list) and runs and isinstance(runs[0], dict):
                title = str(runs[0].get("text", ""))
            out.append((renderer["videoId"], title))
        for value in node.values():
            _video_renderers(value, out, limit)
    elif isinstance(node, list):
        for value in node:
            _video_renderers(value, out, limit)


def find_first_result(song: str, fetch=None) -> tuple[str, str] | None:
    """Return ``(video_id, title)`` of the first suitable result, or None.

    The first result whose title fuzzily matches the requested song wins
    (search order is YouTube's own relevance ranking, so "first suitable"
    stays honest); anything at or above the local-search minimum threshold
    is acceptable if nothing reaches the close-match bar.  None means the
    result could not be determined (offline, consent wall, layout change)
    and the caller falls back to opening the search page.
    """
    try:
        html = (fetch or _fetch_search_html)(song)
    except OSError as exc:
        logger.warning("YouTube search fetch failed: %s", exc)
        return None
    data = _yt_initial_data(html)
    if data is None:
        logger.warning("YouTube search page had no parseable ytInitialData")
        return None
    candidates: list[tuple[str, str]] = []
    _video_renderers(data, candidates)
    if not candidates:
        logger.warning("No video results parsed from the YouTube search page")
        return None
    needle = _normalize(song)
    fallback: tuple[str, str] | None = None
    for video_id, title in candidates:
        if not title:
            continue
        score = fuzz.token_set_ratio(needle, _normalize(title))
        if score >= config.MATCH_CLOSE_THRESHOLD:
            return video_id, title
        if fallback is None and score >= config.MATCH_MIN_THRESHOLD:
            fallback = (video_id, title)
    return fallback


class YouTubeSource(PlaybackSource):
    """Open the first suitable YouTube result in Brave (existing session)."""

    def __init__(self, song: str) -> None:
        super().__init__(
            Capabilities(
                label="YouTube in Brave",
                can_pause=False,        # the tab itself is not directly controllable
                can_read_state=False,   # playback state only via system media session
            )
        )
        self._song = song
        self._first_result: tuple[str, str] | None = None

    @property
    def control_hint(self) -> str | None:
        # Brave exposes a GSMTC session while media plays in a tab.
        return "brave"

    @property
    def control_title(self) -> str | None:
        # The requested song identifies *this* source's media session even
        # when several Brave tabs or players report sessions.
        return self._song

    @property
    def waits_for_playback(self) -> bool:
        # The browser decides whether the watch page autoplays; the countdown
        # must not run until the matching media session is observed Playing.
        return True

    @property
    def search_url(self) -> str:
        return config.YOUTUBE_SEARCH_URL.format(query=quote_plus(self._song))

    @property
    def watch_url(self) -> str | None:
        """Watch page of the first suitable result, or None before play()."""
        if self._first_result is None:
            return None
        return config.YOUTUBE_WATCH_URL.format(video_id=self._first_result[0])

    @property
    def waiting_message(self) -> str:
        if self._first_result is not None:
            _video_id, title = self._first_result
            return (
                f"No local match — opening “{title}” on YouTube in Brave. "
                "The countdown starts once it is playing."
            )
        return (
            f"No local match — the YouTube search for “{self._song}” opened in Brave. "
            "Press play on a result; the countdown starts then."
        )

    def play(self) -> None:
        """Open the first suitable result's watch page in a new Brave tab.

        Falls back to opening the search page whenever the first result
        cannot be determined — the workflow degrades exactly to the old
        behaviour instead of failing.
        """
        self._first_result = find_first_result(self._song)
        url = self.watch_url or self.search_url
        if self._first_result is not None:
            logger.info("First suitable YouTube result: %r (%s)", self._first_result[1], self._first_result[0])
        else:
            logger.info("First result undetermined; opening the search page instead")
        self._open(url)

    def _open(self, url: str) -> None:
        brave = brave_path()
        if brave is not None:
            try:
                # If Brave is already running this process hands the URL to
                # the existing instance and exits; otherwise Brave starts.
                subprocess.Popen([str(brave), url])
                logger.info("Opened %s in Brave", url)
                return
            except OSError as exc:
                raise PlaybackError(f"Could not launch Brave: {exc}") from exc

        # Brave not found — fall back to the system default browser rather
        # than failing the whole workflow.
        if webbrowser.open(url):
            logger.info("Brave not found; opened YouTube in default browser")
            return
        raise PlaybackError("Could not open any browser for the YouTube fallback.")

    def pause(self) -> bool:
        # Honest: no direct handle into the browser tab.  System-level
        # pause of Brave's media session is handled by media_control.
        return False

    def state(self) -> PlaybackState:
        return PlaybackState.UNKNOWN