"""Full live application test (no screenshots — all checks are programmatic).

Drives the real wired application end to end:

1. enter "karma police" + a short duration in the real island UI and start;
2. the countdown must be HELD (paused, full duration) while the file is
   being found, opened, and loaded — never running on Start alone (FIX 2);
3. the app searches the real Music folder and opens the match with the
   machine's actual default audio player; the countdown must begin — with
   the FULL duration — only once that player's session is confirmed Playing;
4. the island's ⏸ must pause both the countdown (immediately, exact
   remaining) and the selected song's media session — never an unrelated
   one; ▶ must resume the song and then continue the countdown from the
   exact remaining time (FIX 1);
5. let the countdown expire → the app must pause the selected session,
   verified through GSMTC;
6. dismiss the expired state, close the window, verify the exit.

Run:  .venv\\Scripts\\python.exe scripts\\probe_app_live.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from music_player import app as app_module
from music_player import settings
from music_player.island import IslandWindow
from music_player.playback import PlaybackOrchestrator
from music_player.timer import TimerState

COUNTDOWN_MINUTES = 1.0  # 60 s — room for the pause/resume checks before expiry
SONG = "karma police"


async def _sessions() -> dict[str, tuple[str, str]]:
    """All current media sessions as {source_id: (status_name, title)}."""
    from music_player.media_control import STATUS_NAMES
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

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
    """Sessions snapshot; tolerates transient COM churn (player relaunch)."""
    for attempt in range(5):
        try:
            return asyncio.run(_sessions())
        except OSError:
            time.sleep(0.5 * (attempt + 1))
    return {}


async def _wait_for(predicate, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    snap = None
    while time.monotonic() < deadline:
        snap = await _sessions()
        if predicate(snap):
            return snap
        await asyncio.sleep(0.5)
    return snap


def _song_session(snap: dict[str, tuple[str, str]], status: str | None = None):
    """The session playing our song (title-matched), whatever the player."""
    from rapidfuzz import fuzz

    for sid, (st, title) in snap.items():
        if status is not None and st != status:
            continue
        if title and fuzz.token_set_ratio(SONG, title.lower()) >= 62:
            return sid
    return None


def _play_song_session() -> bool:
    """try_play the song's session (user simulation) via GSMTC."""
    from rapidfuzz import fuzz
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    async def run() -> bool:
        manager = await SessionManager.request_async()
        for session in manager.get_sessions():
            try:
                props = await session.try_get_media_properties_async()
            except Exception:
                continue
            title = (props.title or "").strip()
            if title and fuzz.token_set_ratio(SONG, title.lower()) >= 62:
                return bool(await session.try_play_async())
        return False

    return asyncio.run(run())


def main() -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

    from PySide6 import QtWidgets

    failures: list[str] = []

    app = QtWidgets.QApplication(sys.argv)
    orchestrator = PlaybackOrchestrator(app)
    window = IslandWindow(settings.load())
    media_controller = app_module.MediaController(app)
    app_module._wire(window, orchestrator, media_controller)

    # Debug tracing of every event the sync logic consumes.
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

    baseline = snapshot()
    print("Sessions before:", baseline)

    # --- 1+2: real workflow start; countdown held from Start (FIX 2) --------
    window.song_input.setText(SONG)
    window.minutes_input.setValue(COUNTDOWN_MINUTES)
    window.start_workflow()
    pump()

    held = window.timer.state is TimerState.PAUSED
    remaining_at_start = window.timer.remaining_seconds()
    print(
        f"Countdown held right after Start: {held}, "
        f"remaining={remaining_at_start:.1f}s of {COUNTDOWN_MINUTES * 60:.0f}s"
    )
    if not held:
        failures.append("countdown was not held from the moment Start was pressed")
    elif remaining_at_start < COUNTDOWN_MINUTES * 60 - 5:
        failures.append(f"duration consumed at Start: {remaining_at_start:.1f}s")

    print("Waiting for the local file to open…")
    deadline = time.monotonic() + 20
    opened = False
    while time.monotonic() < deadline:
        pump()
        if orchestrator.current_source is not None:
            opened = True
            break
        time.sleep(0.1)
    print("Local source started:", opened)
    if not opened:
        failures.append("local playback never started")

    # --- 3: gate — countdown begins with the FULL duration once Playing ----
    snap = asyncio.run(_wait_for(lambda s: _song_session(s, "Playing") is not None, 30))
    print("Sessions while playing:", snap)
    our_sid = _song_session(snap or {}, "Playing")
    if our_sid is None:
        failures.append(f"no playing session for the song appeared (sessions: {snap})")

    running = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pump()
        if window.timer.state is TimerState.RUNNING:
            running = True
            break
        time.sleep(0.1)
    remaining_running = window.timer.remaining_seconds()
    print(
        f"Countdown running after playback confirmed: {running}, "
        f"remaining={remaining_running:.1f}s"
    )
    if not running:
        failures.append("countdown did not start once playback was confirmed")
    elif remaining_running < COUNTDOWN_MINUTES * 60 - 10:
        failures.append(
            f"countdown did not begin with the full duration: {remaining_running:.1f}s"
        )

    # --- 4: island ⏸/▶ must control the selected song (FIX 1) -----------------
    if running and our_sid is not None:
        time.sleep(2.0)  # let a little duration burn
        pump()
        others_before = {
            sid: st for sid, (st, title) in snapshot().items() if sid != our_sid
        }
        print("Unrelated sessions before the island pause:", others_before)

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

        paused_snap = asyncio.run(
            _wait_for(lambda s: _song_session(s, "Paused") is not None, 15)
        )
        song_paused = _song_session(paused_snap or {}, "Paused") is not None
        print("Selected song's session paused by the island ⏸ (GSMTC):", song_paused)
        if not song_paused:
            failures.append("island ⏸ did not pause the selected song's session")

        snap_after = snapshot()
        disturbed = [
            sid
            for sid, st in others_before.items()
            if st == "Playing" and snap_after.get(sid, ("", ""))[0] == "Paused"
        ]
        print("Unrelated playing sessions paused by mistake:", disturbed)
        if disturbed:
            failures.append(f"unrelated sessions were paused: {disturbed}")

        time.sleep(1.5)  # wall-clock passes while paused: nothing may be lost
        window.pause_button.click()  # ▶ in the real UI
        pump()
        if window.timer.state is TimerState.RUNNING:
            failures.append("countdown resumed before playback was confirmed Playing")
        resumed_snap = asyncio.run(
            _wait_for(lambda s: _song_session(s, "Playing") is not None, 15)
        )
        song_resumed = _song_session(resumed_snap or {}, "Playing") is not None
        resumed_running = False
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            pump()
            if window.timer.state is TimerState.RUNNING:
                resumed_running = True
                break
            time.sleep(0.1)
        remaining_after = window.timer.remaining_seconds()
        print(
            f"Island ▶: song resumed={song_resumed}, countdown running={resumed_running}, "
            f"continuing from {remaining_after:.1f}s (was {remaining_at_pause:.1f}s at pause)"
        )
        if not (song_resumed and resumed_running):
            failures.append("island ▶ did not resume the selected song + countdown")
        elif remaining_after > remaining_at_pause + 1.0:
            failures.append("countdown did not continue from the exact remaining time")

    # --- 5: countdown expiry --------------------------------------------------
    print("Waiting for countdown expiry…")
    resumed_after_suspend = 0
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        pump()
        if window.timer.state is TimerState.EXPIRED:
            break
        if window.timer.state is TimerState.PAUSED:
            # Environmental: the player paused/suspended itself (observed
            # with UWP players) — the sync pause of the countdown is the
            # app's correct reaction, not a bug.  Resume the song (as the
            # user would) so the countdown can still run out.
            print("[probe] playback paused externally — resuming the song")
            _play_song_session()
            resumed_after_suspend += 1
            time.sleep(1.0)
        time.sleep(0.1)
    print("Countdown expired:", window.timer.state is TimerState.EXPIRED)
    print(f"(song resumed by the probe after external pauses: {resumed_after_suspend}x)")
    if window.timer.state is not TimerState.EXPIRED:
        failures.append("countdown never expired")

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        pump()
        if "paus" in window.status_label.text().lower():
            break
        time.sleep(0.1)
    print("Status after expiry:", window.status_label.text())

    # --- 5b: verify through GSMTC that the song's session is really paused ----
    if our_sid is not None:
        paused_snap = asyncio.run(
            _wait_for(lambda s: s.get(our_sid, ("", ""))[0] == "Paused", 8)
        )
        paused_now = (paused_snap or {}).get(our_sid, ("", ""))[0] == "Paused"
        print("Selected session after expiry pause:", paused_snap)
        if not paused_now:
            failures.append(f"selected session not paused after expiry (sessions: {paused_snap})")

    # --- 6: dismiss, close ------------------------------------------------------
    window.cancel_button.click()  # dismiss the expired state
    window.close()
    pump()
    print("Window closed:", not window.isVisible())

    # Best-effort cleanup for classic-exe players we started; UWP players
    # (e.g. Microsoft Media Player) suspend themselves and are left alone.
    if our_sid is not None and our_sid.lower().endswith(".exe"):
        import subprocess

        subprocess.run(["taskkill", "/IM", Path(our_sid).name, "/F"], capture_output=True)
        print("Closed player process", our_sid, "(started by this test)")

    if failures:
        print("FAIL:", failures)
        return 1
    print(
        "PASS: full live workflow verified "
        "(search → open → held until confirmed Playing with the full duration "
        "→ island ⏸/▶ pause-resume of the selected song → expire → pause → exit)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())