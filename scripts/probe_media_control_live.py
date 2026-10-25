"""Real-world GSMTC integration check.

Launches the user's default media player (PotPlayer) on the real test song,
then verifies through the Windows media-session API that it can be
observed, paused, and resumed.  Finally closes the player process that this
script itself started (nothing else is touched).

Run:  .venv\\Scripts\\python.exe scripts\\probe_media_control_live.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

SONG = Path(r"C:\Users\ygyg4\OneDrive\Music\Radiohead - Karma Police.mp3")
POTPLAYER = Path(r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe")

STATUS_NAMES = {0: "Closed", 1: "Opened", 2: "Changing", 3: "Stopped", 4: "Playing", 5: "Paused"}


def status_name(status) -> str:
    return STATUS_NAMES.get(int(status), str(status))


async def wait_status(session, wanted: str, timeout_s: float = 4.0) -> str | None:
    """Poll until the session reports *wanted* (players report with lag)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if status_name(session.get_playback_info().playback_status) == wanted:
            return wanted
        await asyncio.sleep(0.25)
    return status_name(session.get_playback_info().playback_status)


async def find_session(manager, needle: str):
    sessions = manager.get_sessions()
    for s in sessions:
        if needle.lower() in s.source_app_user_model_id.lower():
            return s
    return None


async def main() -> int:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    if not SONG.exists():
        print("FAIL: test song missing:", SONG)
        return 1

    print("Launching", POTPLAYER.name, "with", SONG.name)
    proc = subprocess.Popen([str(POTPLAYER), str(SONG)])
    try:
        manager = await SessionManager.request_async()

        session = None
        for _ in range(20):  # wait up to 10 s for the session to appear
            session = await find_session(manager, "potplayer")
            if session is not None:
                break
            await asyncio.sleep(0.5)
        if session is None:
            print("FAIL: no PotPlayer media session appeared (GSMTC unsupported by player?)")
            return 1

        print("Session found:", session.source_app_user_model_id)

        # Wait until the player actually reports Playing (PotPlayer registers
        # its session as "Opened" before the file starts).
        playing = False
        for _ in range(30):  # up to 15 s
            if status_name(session.get_playback_info().playback_status) == "Playing":
                playing = True
                break
            await asyncio.sleep(0.5)
        print("Reached Playing state:", playing)
        if not playing:
            print("Current status:", status_name(session.get_playback_info().playback_status))
            print("NOTE: player did not auto-start playback; pause/resume not exercised.")
            return 3

        ok = await session.try_pause_async()
        paused = await wait_status(session, "Paused")
        print("try_pause:", ok, "-> status now:", paused)

        ok = await session.try_play_async()
        resumed = await wait_status(session, "Playing")
        print("try_play:", ok, "-> status now:", resumed)

        props = await session.try_get_media_properties_async()
        print("Now playing:", repr(props.title), "-", repr(props.artist))

        if paused == "Paused" and resumed == "Playing":
            print("PASS: pause and resume control verified over GSMTC")
            return 0
        print("PARTIAL: control outcomes above (pause:", paused == "Paused", "resume:", resumed == "Playing", ")")
        return 2
    finally:
        proc.terminate()  # only the process this script launched
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("Player process closed (pid", proc.pid, ")")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))