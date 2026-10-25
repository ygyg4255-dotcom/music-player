"""One-off: list current GSMTC sessions (id, status, title) for live probing."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def main() -> None:
    from music_player.media_control import STATUS_NAMES
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )

    manager = await SessionManager.request_async()
    for session in manager.get_sessions():
        info = session.get_playback_info()
        title = ""
        try:
            props = await session.try_get_media_properties_async()
            title = (props.title or "").strip()
        except Exception:
            pass
        print(str(session.source_app_user_model_id), STATUS_NAMES.get(int(info.playback_status)), repr(title))


if __name__ == "__main__":
    asyncio.run(main())