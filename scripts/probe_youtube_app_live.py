"""Full live application test of the YouTube fallback + countdown sync.

No screenshots — every check is programmatic (window titles, GSMTC states,
timer states, status lines).  Drives the real wired application:

1. type a song that is NOT in the local Music folder + a short duration,
   press Start in the real island UI;
2. the app falls back to YouTube and opens the first result's watch page
   in the existing Brave session;
3. the countdown must be PAUSED at (nearly) its full remaining time until
   the matching Brave media session is observed Playing — then it runs;
4. an externally-triggered pause of the YouTube tab (simulating the user
   pausing the video) must pause the countdown; resuming continues from
   the remaining time;
5. expiry must pause the selected song's session — and nothing else —
   without closing Brave or any other app;
6. clean exit, Brave preserved.

Run:  .venv\\Scripts\\python.exe scripts\\probe_youtube_app_live.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music_player import app as app_module
from music_player import local_search, settings
from music_player.island import IslandWindow
from music_player.playback import PlaybackOrchestrator
from music_player.timer import TimerState

SONG = "street spirit fade out radiohead"
COUNTDOWN_MINUTES = 1.0  # 60 s — room for the pause/resume checks before expiry


def _brave_running() -> bool:
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq brave.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    return any(line.lower().startswith('"brave.exe') for line in out.splitlines())


async def _sessions() -> list[tuple[str, str, str]]:
    """All sessions as a LIST of (source_id, status, title) — no dedup.

    Brave can expose several sessions that share the same source id (one
    per tab), so a dict keyed by id would silently hide some of them.
    """
    from music_player.media_control import STATUS_NAMES
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    manager = await SessionManager.request_async()
    out: list[tuple[str, str, str]] = []
    for session in manager.get_sessions():
        info = session.get_playback_info()
        title = ""
        try:
            props = await session.try_get_media_properties_async()
            title = (props.title or "").strip()
        except Exception:
            pass
        out.append((
            str(session.source_app_user_model_id),
            STATUS_NAMES.get(int(info.playback_status), "Unknown"),
            title,
        ))
    return out


def snapshot() -> list[tuple[str, str, str]]:
    """Sessions snapshot; tolerates transient COM churn (e.g. tab churn)."""
    for attempt in range(5):
        try:
            return asyncio.run(_sessions())
        except OSError:
            time.sleep(0.5 * (attempt + 1))
    return []


async def _control_session(control: str, title_needle: str) -> bool:
    """try_pause/try_play the Brave session playing our song (user simulation)."""
    from rapidfuzz import fuzz

    from music_player.media_control import STATUS_NAMES

    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    manager = await SessionManager.request_async()
    for session in manager.get_sessions():
        sid = str(session.source_app_user_model_id)
        if "brave" not in sid.lower():
            continue
        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            continue
        title = (props.title or "").strip()
        if not title:
            continue
        if fuzz.token_set_ratio(title_needle.lower(), title.lower()) >= 62:
            if control == "pause":
                return bool(await session.try_pause_async())
            return bool(await session.try_play_async())
    return False


def user_pause_song() -> bool:
    return asyncio.run(_control_session("pause", SONG))


def user_resume_song() -> bool:
    return asyncio.run(_control_session("play", SONG))


def _song_status(snap: list[tuple[str, str, str]], status: str) -> bool:
    """Whether the song's Brave session reports *status* (title-matched)."""
    return any(
        "brave" in sid.lower() and st == status and "street spirit" in title.lower()
        for sid, st, title in snap
    )


def wait(predicate, timeout_s: float, pump) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pump()
        if predicate():
            return True
        time.sleep(0.1)
    pump()
    return predicate()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    from PySide6 import QtWidgets

    failures: list[str] = []

    # Safety: this song must NOT match anything in the local Music folder.
    local = local_search.search(SONG)
    print("Local search result for the probe song:", local)
    if local is not None:
        print("ABORT: probe song unexpectedly matches locally; pick another song")
        return 1

    brave_before = _brave_running()
    print("Brave running before:", brave_before)

    app = QtWidgets.QApplication(sys.argv)
    orchestrator = PlaybackOrchestrator(app)
    window = IslandWindow(settings.load())
    media_controller = app_module.MediaController(app)
    app_module._wire(window, orchestrator, media_controller)

    # Trace every event the sync logic consumes.
    media_controller.tracked_state.connect(
        lambda s, st: print(f"[trace] tracked_state: {s} -> {st}", flush=True)
    )
    media_controller.session_lost.connect(lambda s: print(f"[trace] session_lost: {s}", flush=True))
    media_controller.pause_result.connect(
        lambda s, ok, m: print(f"[trace] pause_result: {s} ok={ok} {m}", flush=True)
    )
    media_controller.control_result.connect(
        lambda s, ok, m: print(f"[trace] control_result: {s} ok={ok} {m}", flush=True)
    )
    window.timer.started.connect(lambda d: print(f"[trace] timer started {d}s", flush=True))
    window.timer.paused.connect(lambda r: print(f"[trace] timer paused @{r:.1f}s", flush=True))
    window.timer.resumed.connect(lambda r: print(f"[trace] timer resumed @{r:.1f}s", flush=True))
    window.timer.expired.connect(lambda: print("[trace] timer expired", flush=True))
    orchestrator.status.connect(lambda m, e: print(f"[trace] status: {m}", flush=True))

    pump = app.processEvents

    # --- 1: start the workflow in the real UI -------------------------------
    window.song_input.setText(SONG)
    window.minutes_input.setValue(COUNTDOWN_MINUTES)
    window.start_workflow()

    # --- 2+3: fallback opens; countdown gated until Playing ------------------
    gated = wait(
        lambda: window.timer.state is TimerState.PAUSED
        and "countdown starts" in window.status_label.text(),
        timeout_s=30,
        pump=pump,
    )
    print("Countdown gated (paused, waiting for playback):", gated)
    print("Status line:", window.status_label.text())
    remaining_gated = window.timer.remaining_seconds()
    print(f"Remaining while gated: {remaining_gated:.1f}s of {COUNTDOWN_MINUTES * 60:.0f}s")
    if not gated:
        failures.append("countdown was not gated while YouTube playback unconfirmed")
    elif remaining_gated < COUNTDOWN_MINUTES * 60 - 10:
        failures.append(f"too much duration consumed before playback: {remaining_gated:.1f}s")

    # The countdown must stay gated until the session actually plays.
    playing = wait(
        lambda: _song_status(snapshot(), "Playing"),
        timeout_s=60,
        pump=pump,
    )
    print("Song playing in Brave (autoplay confirmed):", playing)
    running = wait(
        lambda: window.timer.state is TimerState.RUNNING, timeout_s=10, pump=pump
    )
    print("Countdown running after playback confirmed:", running)
    if not running:
        failures.append("countdown did not start after playback was confirmed")
    else:
        # FIX 2 live check: the countdown began with the FULL duration once
        # playback was confirmed (startup seconds must not consume it).
        remaining_running = window.timer.remaining_seconds()
        print(
            f"Remaining once running: {remaining_running:.1f}s "
            f"of {COUNTDOWN_MINUTES * 60:.0f}s"
        )
        if remaining_running < COUNTDOWN_MINUTES * 60 - 10:
            failures.append(
                f"countdown did not begin with the full duration: {remaining_running:.1f}s"
            )

    # --- 4: island ⏸/▶ must control the selected song (FIX 1) ----------------
    if running:
        time.sleep(2.0)  # let a little duration burn
        pump()
        others_before = [
            (sid, title, status)
            for sid, status, title in snapshot()
            if "street spirit" not in title.lower()
        ]
        print(f"Unrelated sessions before the island pause: {others_before}")

        window.pause_button.click()  # ⏸ in the real UI
        pump()
        island_paused = window.timer.state is TimerState.PAUSED
        remaining_at_pause = window.timer.remaining_seconds()
        print(
            f"Island ⏸: countdown paused immediately={island_paused}, "
            f"remaining={remaining_at_pause:.1f}s"
        )
        if not island_paused:
            failures.append("island ⏸ did not pause the countdown immediately")

        song_paused = wait(
            lambda: _song_status(snapshot(), "Paused"),
            timeout_s=15,
            pump=pump,
        )
        print("Selected song's Brave session paused by the island ⏸:", song_paused)
        if not song_paused:
            failures.append("island ⏸ did not pause the selected song's session")

        snap_after = snapshot()
        after_keys = {(sid, title): status for sid, status, title in snap_after}
        disturbed = [
            f"{sid} ({title})"
            for sid, title, status in others_before
            if status == "Playing" and after_keys.get((sid, title)) == "Paused"
        ]
        print("Unrelated playing sessions paused by mistake:", disturbed)
        if disturbed:
            failures.append(f"unrelated sessions were paused: {disturbed}")

        time.sleep(1.5)  # wall-clock passes while paused: nothing may be lost
        window.pause_button.click()  # ▶ in the real UI
        pump()
        if window.timer.state is TimerState.RUNNING:
            failures.append("countdown resumed before playback was confirmed Playing")
        song_resumed = wait(
            lambda: _song_status(snapshot(), "Playing"),
            timeout_s=15,
            pump=pump,
        )
        resumed_running = wait(
            lambda: window.timer.state is TimerState.RUNNING, timeout_s=15, pump=pump
        )
        remaining_after = window.timer.remaining_seconds()
        print(
            f"Island ▶: song resumed={song_resumed}, countdown running={resumed_running}, "
            f"continuing from {remaining_after:.1f}s (was {remaining_at_pause:.1f}s at pause)"
        )
        if not (song_resumed and resumed_running):
            failures.append("island ▶ did not resume the selected song + countdown")
        elif remaining_after > remaining_at_pause + 1.0:
            failures.append("countdown did not continue from the exact remaining time")

    # --- 5: external pause of the video syncs the countdown -------------------
    if running:
        time.sleep(2.0)  # let some duration burn
        pump()
        before_pause = window.timer.remaining_seconds()
        print(f"User pauses the YouTube tab (simulated via GSMTC)… remaining={before_pause:.1f}s")
        if not user_pause_song():
            failures.append("could not simulate the user pause via GSMTC")
        synced_pause = wait(
            lambda: window.timer.state is TimerState.PAUSED, timeout_s=15, pump=pump
        )
        print("Countdown paused in sync with the video:", synced_pause)
        if not synced_pause:
            failures.append("video pause did not pause the countdown")

        time.sleep(1.5)  # wall-clock passes while paused: nothing may be lost
        print("User resumes the video…")
        if not user_resume_song():
            failures.append("could not simulate the user resume via GSMTC")
        synced_resume = wait(
            lambda: window.timer.state is TimerState.RUNNING, timeout_s=15, pump=pump
        )
        after_resume = window.timer.remaining_seconds()
        print(
            f"Countdown resumed: {synced_resume}, continuing from {after_resume:.1f}s "
            f"(was {before_pause:.1f}s at pause)"
        )
        if not synced_resume:
            failures.append("video resume did not resume the countdown")
        elif after_resume > before_pause + 0.5:
            failures.append("countdown did not continue from the exact remaining time")

    # --- 6: expiry pauses the selected song, nothing else --------------------
    print("Waiting for countdown expiry…")
    resumed_after_suspend = 0
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        pump()
        if window.timer.state is TimerState.EXPIRED:
            break
        if window.timer.state is TimerState.PAUSED:
            # Environmental: the tab paused itself or was paused externally —
            # the sync pause of the countdown is correct; resume the video so
            # the countdown can still run out.
            print("[probe] playback paused externally — resuming the video")
            user_resume_song()
            resumed_after_suspend += 1
            time.sleep(1.0)
        time.sleep(0.1)
    expired = window.timer.state is TimerState.EXPIRED
    print("Countdown expired:", expired)
    print(f"(video resumed by the probe after external pauses: {resumed_after_suspend}x)")
    if not expired:
        failures.append("countdown never expired")
    else:
        paused_song = wait(
            lambda: _song_status(snapshot(), "Paused"),
            timeout_s=15,
            pump=pump,
        )
        print("Selected song paused after expiry (GSMTC):", paused_song)
        if not paused_song:
            failures.append("the selected song was not paused after expiry")

    # --- 7: clean exit; Brave preserved ---------------------------------------
    window.cancel_button.click()  # dismiss the expired state
    window.close()
    pump()
    print("Window closed:", not window.isVisible())

    brave_after = _brave_running()
    print("Brave running after:", brave_after)
    if brave_before and not brave_after:
        failures.append("Brave was closed by the app — forbidden")

    if failures:
        print("FAIL:", failures)
        return 1
    print(
        "PASS: full live YouTube workflow verified "
        "(no local match → first result opened → gated until Playing with the full "
        "duration → island ⏸/▶ pause-resume of the selected song → sync pause/resume "
        "→ expiry pause → Brave preserved)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())