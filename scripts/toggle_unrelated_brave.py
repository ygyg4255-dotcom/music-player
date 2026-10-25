"""Resume/pause the user's unrelated Brave video session for live probing.

Restores the unrelated Brave media session to Playing (its earlier state
during this work) so the live probes can verify the app never pauses an
unrelated PLAYING session.  Afterwards it can pause it again to leave the
machine as found.

Run:  .venv\\Scripts\\python.exe scripts\\toggle_unrelated_brave.py resume|pause
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

NEEDLE = "claude code free"  # the unrelated video (Arabic) playing in Brave


async def toggle(control: str) -> int:
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
        if NEEDLE in title.lower():
            if control == "resume":
                ok = bool(await session.try_play_async())
            else:
                ok = bool(await session.try_pause_async())
            print(f"{control} ok={ok} on {sid!r} ({title!r})")
            return 0 if ok else 1
    print("unrelated Brave session not found")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(toggle(sys.argv[1] if len(sys.argv) > 1 else "resume")))