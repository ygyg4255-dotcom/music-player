"""Diagnostic probe: list current Windows media sessions (GSMTC).

Run:  .venv\\Scripts\\python.exe scripts\\probe_media_sessions.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def main() -> None:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    manager = await SessionManager.request_async()
    session = manager.get_current_session()
    print("Current session:", session.source_app_user_model_id if session else None)

    sessions = manager.get_sessions()
    print(f"{sessions.size} media session(s):")
    for s in sessions:
        info = s.get_playback_info()
        props = await s.try_get_media_properties_async()
        title = props.title if props else "?"
        artist = props.artist if props else "?"
        print(
            f"  - {s.source_app_user_model_id}: status={info.playback_status} "
            f"title={title!r} artist={artist!r} controls={info.controls}"
        )


if __name__ == "__main__":
    asyncio.run(main())