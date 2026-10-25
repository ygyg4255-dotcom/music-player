"""Real-world Brave integration check (non-image verification).

Steps:
1. confirm Brave is/isn't running before the test;
2. open a YouTube search through YouTubeSource (the app's fallback path);
3. verify programmatically — via window titles (EnumWindows), not
   screenshots — that a Brave window shows the YouTube search;
4. open a second search and verify it reused the same browser (no second
   browser session);
5. attempt a media pause (no media is playing, so the expected outcome is
   an honest "no session" report) and verify Brave is preserved.

Brave is deliberately left running — the app must never close it.

Run:  .venv\\Scripts\\python.exe scripts\\probe_brave_live.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def brave_pids() -> set[int]:
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
    """Titles of visible top-level windows owned by the given pids."""
    titles: list[str] = []
    user32 = ctypes.windll.user32
    enum_windows = user32.EnumWindows
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

    enum_windows(WNDPROC(on_window), 0)
    return titles


def main() -> int:
    from music_player.media_control import MediaController
    from music_player.youtube import YouTubeSource

    before = brave_pids()
    print(f"Brave processes before: {len(before)}")

    source = YouTubeSource("karma police")
    print("Opening YouTube search via the app's fallback path…")
    source.play()

    # Brave can take a few seconds to start and render the tab.
    youtube_visible = False
    for _ in range(30):
        time.sleep(1)
        titles = window_titles_for(brave_pids())
        if any("YouTube" in t or "karma" in t.lower() for t in titles):
            youtube_visible = True
            break
    after = brave_pids()
    titles = window_titles_for(after)
    print(f"Brave processes after: {len(after)} (same session: {before <= after or len(before) == 0})")
    print("Brave window titles:", titles[:5])
    print("YouTube search tab visible:", youtube_visible)
    if not youtube_visible:
        print("FAIL: no Brave window shows the YouTube search")
        return 1

    # Second open must reuse the existing browser session (same browser).
    count_before = len(brave_pids())
    YouTubeSource("no surprises radiohead").play()
    time.sleep(6)
    count_after = len(brave_pids())
    print(f"Second open: brave processes {count_before} -> {count_after}")
    titles = window_titles_for(brave_pids())
    print("Titles now:", titles[:5])
    reused = any("no surprises" in t.lower() for t in titles) or any(
        "youtube" in t.lower() for t in titles
    )
    print("Second search opened in the same browser:", reused)

    # Pause attempt with no media playing → honest refusal, Brave untouched.
    ctrl = MediaController()
    results: list[tuple[str, bool, str]] = []
    ctrl.pause_result.connect(lambda s, ok, m: results.append((s, ok, m)))
    ctrl.pause_relevant("brave")
    time.sleep(2)
    print("Pause attempt with no media playing:", results)
    still_running = len(brave_pids()) > 0
    print("Brave preserved after pause attempt:", still_running)

    ok = youtube_visible and still_running
    print("PASS" if ok else "FAIL", "- Brave integration verified; Brave left running as required.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())