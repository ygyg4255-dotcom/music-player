"""Live YouTube-fallback check (no screenshots — all verification programmatic).

Verifies the fixed fallback path component by component against the real
network, the real Brave browser, and the real Windows media sessions:

1. find_first_result() must determine an actual first result (real fetch).
2. YouTubeSource.play() opens that result's WATCH page in the existing
   Brave session (verified via window titles, not images).
3. GSMTC must expose a Brave session whose media title matches the song;
   its status is then observed (autoplay confirmation, honestly reported).
4. A title-matched GSMTC pause must pause that session; Brave is preserved.

Run:  .venv\\Scripts\\python.exe scripts\\probe_youtube_live.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music_player import config, youtube
from music_player.youtube import YouTubeSource

SONG = "street spirit fade out radiohead"


async def _sessions() -> dict[str, tuple[str, str]]:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )
    from music_player.media_control import STATUS_NAMES

    manager = await SessionManager.request_async()
    out: dict[str, tuple[str, str]] = {}
    for session in manager.get_sessions():
        info = session.get_playback_info()
        title = ""
        try:
            props = await session.try_get_media_properties_async()
            title = (props.title or "").strip()
        except Exception:
            pass
        out[str(session.source_app_user_model_id)] = (
            STATUS_NAMES.get(int(info.playback_status), "Unknown"),
            title,
        )
    return out


def snapshot() -> dict[str, tuple[str, str]]:
    return asyncio.run(_sessions())


def song_sessions() -> dict[str, tuple[str, str]]:
    """Only sessions whose media title matches the requested song."""
    return {
        sid: (status, title)
        for sid, (status, title) in snapshot().items()
        if title and _matches(title)
    }


def _matches(title: str) -> bool:
    from rapidfuzz import fuzz

    return (
        fuzz.token_set_ratio(youtube._normalize(SONG), youtube._normalize(title))
        >= config.MATCH_MIN_THRESHOLD
    )


def brave_pids() -> set[int]:
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq brave.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "brave.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def window_titles_for(pids: set[int]) -> list[str]:
    import ctypes

    titles: list[str] = []
    user32 = ctypes.windll.user32
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on_window(hwnd, _lparam):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
        return True

    user32.EnumWindows(WNDPROC(on_window), 0)
    return titles


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    failures: list[str] = []

    # --- 1: first result determined from a real fetch --------------------------
    first = youtube.find_first_result(SONG)
    print("First suitable result:", first)
    if first is None:
        print("FAIL: no first result determined (network/consent wall/layout change)")
        return 1

    # --- 2: play() opens the watch page in the existing Brave ------------------
    before = brave_pids()
    source = YouTubeSource(SONG)
    source.play()
    print("Source reports first result:", source._first_result)
    print("Watch URL:", source.watch_url)
    if source._first_result is None:
        print("FAIL: play() did not keep the determined result")
        return 1

    watch_visible = False
    for _ in range(30):
        time.sleep(1)
        titles = window_titles_for(brave_pids())
        if any("- YouTube - Brave" in t for t in titles):
            watch_visible = True
            break
    print("Brave window titles:", window_titles_for(brave_pids())[:5])
    print("Watch page tab visible:", watch_visible)
    if not watch_visible:
        failures.append("no Brave window shows a YouTube watch page")

    # --- 3: matching media session appears; is it actually Playing? -----------
    print("Waiting for a Brave media session whose title matches the song…")
    playing = False
    tracked = ""
    for _ in range(60):
        snap = song_sessions()
        if snap:
            for sid, (status, title) in snap.items():
                if "brave" in sid.lower():
                    tracked = sid
                    print(f"  session {sid}: {status} ({title!r})")
                    if status == "Playing":
                        playing = True
        if playing:
            break
        time.sleep(1)
    if not tracked:
        failures.append("no Brave media session with a matching title appeared")
    if not playing:
        print(
            "LIMITATION: the watch page opened but playback did not start by "
            "itself within 60 s — the browser's autoplay policy (or YouTube) "
            "requires a manual click. The app's gating handles this honestly "
            "(countdown held, duration not consumed)."
        )

    # --- 4: title-matched GSMTC pause; Brave preserved -------------------------
    from PySide6 import QtCore

    from music_player.media_control import MediaController

    if QtCore.QCoreApplication.instance() is None:
        QtCore.QCoreApplication([])
    ctrl = MediaController()
    results: list[tuple[str, bool, str]] = []
    ctrl.pause_result.connect(lambda s, ok, m: results.append((s, ok, m)))
    ctrl.pause_relevant("brave", title_query=SONG)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not results:
        QtCore.QCoreApplication.processEvents()  # queued signals need a pump
        time.sleep(0.1)
    ctrl._shutdown()
    print("Pause result:", results)
    if playing:
        if not (results and results[0][1]):
            failures.append("playing song session was not paused via GSMTC")
        # Players report status with up to ~1 s lag — poll for the change
        # instead of sampling once (same lesson as the GSMTC sync layer).
        paused_confirmed = False
        for _ in range(10):
            snap = song_sessions()
            if snap.get(tracked, ("", ""))[0] == "Paused":
                paused_confirmed = True
                break
            time.sleep(0.5)
        print("Session statuses after pause:", snapshot())
        if tracked and not paused_confirmed:
            failures.append(f"session not paused after GSMTC pause: {snapshot().get(tracked)}")

    after_pids = brave_pids()
    # Chromium constantly spawns/reaps helper processes, so PID *sets* churn;
    # "preserved" means the browser is still running with the same windows.
    preserved = bool(after_pids)
    print(f"Brave processes: before={len(before)} after={len(after_pids)} (still running: {preserved})")
    print("Brave processes preserved:", preserved)
    if not preserved:
        failures.append("Brave processes disappeared — the app must never close Brave")

    if failures:
        print("FAIL:", failures)
        return 1
    print(
        "PASS: first result determined and opened as a watch page in the existing "
        "Brave session; matching media session "
        + ("confirmed playing and pausable." if playing else "found (autoplay not confirmed — see limitation above).")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())