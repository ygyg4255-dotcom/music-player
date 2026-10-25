"""Windows media-session control (Global System Media Transport Controls).

Real, verified behavior on this machine (PotPlayer, Windows 11):

* every media-capable app can expose a *session* with a playback status;
* ``try_pause_async`` / ``try_play_async`` pause/resume *if the app accepts*;
* apps report their status with a lag of up to ~1 s, so changes must be
  confirmed by polling rather than sampled once.

The controller runs its WinRT calls on a dedicated asyncio thread and
never blocks the GUI thread.  All *decisions* live in the pure function
:func:`pick_session` so they are unit-testable without Windows media
sessions.

Honesty rules baked into the design:

* we never pause *all* sessions — a target must be unambiguous (either it
  matches the app the music player itself launched, or exactly one session
  is playing, or exactly one session exists at all);
* if ``winrt`` is unavailable, everything degrades to clear "cannot"
  messages instead of pretending control works.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
from dataclasses import dataclass

from PySide6 import QtCore
from rapidfuzz import fuzz
from rapidfuzz.utils import default_process

from . import config

logger = logging.getLogger(__name__)

#: GSMTC playback statuses mapped to plain names.
STATUS_NAMES = {0: "Closed", 1: "Opened", 2: "Changing", 3: "Stopped", 4: "Playing", 5: "Paused"}

POLL_INTERVAL_S = 2.0


@dataclass(frozen=True)
class SessionInfo:
    """A snapshot of one media session."""

    source_id: str
    status: str  # one of STATUS_NAMES values
    title: str = ""
    artist: str = ""


def _normalize(text: str) -> str:
    """Casefold + strip diacritics + collapse whitespace (as in local_search)."""
    return " ".join(default_process(text).split())


def _title_matches(query: str, title: str) -> bool:
    """Whether a session's reported media title matches the requested song."""
    return fuzz.token_set_ratio(_normalize(query), _normalize(title)) >= config.MATCH_MIN_THRESHOLD


def pick_session(
    infos: list[SessionInfo],
    hint: str | None,
    tracked_id: str | None = None,
    title_query: str | None = None,
) -> SessionInfo | None:
    """Choose the single relevant session, or None when ambiguous.

    Rules (in order):

    1. A previously tracked id is re-picked by identity — but when a title
       query is known, the tracked session must still *match that title*:
       an app can reuse its single session for different media (observed
       live with Brave switching to an unrelated tab's video).  A session
       with the same id but a different title is NOT the selected song; a
       fresh hint+title pick then applies.  A missing tracked id is None
       (session lost), and an empty reported title is trusted (properties
       unavailable).
    2. An explicit *hint* (e.g. "potplayermini64", "brave") narrows the
       candidates to sessions whose source id contains it.
    3. With a *title_query* (the requested song), only candidates whose
       reported media title fuzzily matches are eligible.  If none
       matches, None is returned — an unrelated playing session is never
       selected just because it is there.
    4. Among candidates, a *Playing* one is preferred.
    5. Exactly one candidate → chosen.  Anything else → None (never guess
       when multiple sessions could be the user's music).
    """
    if tracked_id is not None:
        tracked = None
        for info in infos:
            if info.source_id == tracked_id:
                tracked = info
                break
        if tracked is None:
            return None
        if not title_query or not tracked.title or _title_matches(title_query, tracked.title):
            return tracked
        # Same app id, different media: fall through and try to find the
        # selected song by hint + title instead of hijacking this session.

    candidates = infos
    if hint:
        needle = hint.lower()
        narrowed = [i for i in infos if needle in i.source_id.lower()]
        if narrowed:
            candidates = narrowed

    if title_query:
        matches = [i for i in candidates if i.title and _title_matches(title_query, i.title)]
        if not matches:
            logger.info("No media session title matches %r; refusing to pick", title_query)
            return None
        # A unique title match is the song this app opened even while it is
        # still paused/loading — track it so its Playing report arrives.
        if len(matches) == 1:
            return matches[0]
        playing = [i for i in matches if i.status == "Playing"]
        if len(playing) == 1:
            return playing[0]
        logger.info("Multiple sessions match the title %r; refusing to pick", title_query)
        return None

    playing = [i for i in candidates if i.status == "Playing"]
    if len(playing) == 1:
        return playing[0]
    if playing:  # several playing candidates → ambiguous
        logger.info("Multiple playing media sessions match hint %r; refusing to pick", hint)
        return None
    if len(candidates) == 1:
        return candidates[0]
    return None


class _WinRtBackend:
    """Thin winrt wrapper; only touched on the controller's asyncio thread."""

    def __init__(self) -> None:
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        self._session_manager_class = SessionManager
        self._manager = None

    async def _get_manager(self, fresh: bool = False):
        if fresh:
            self._manager = None
        if self._manager is None:
            self._manager = await self._session_manager_class.request_async()
        return self._manager

    async def get_sessions(self) -> list[SessionInfo]:
        # The cached manager can go stale when sessions churn (observed
        # with a UWP player resuming from suspension): re-request once
        # before giving up so monitoring never goes blind silently.
        last_exc: Exception | None = None
        for fresh in (False, True):
            try:
                manager = await self._get_manager(fresh=fresh)
                out: list[SessionInfo] = []
                for session in manager.get_sessions():
                    info = session.get_playback_info()
                    title = ""
                    try:
                        # The media title identifies *which* song the session
                        # is playing — needed to pick the session belonging
                        # to the app-opened song among several candidates.
                        props = await session.try_get_media_properties_async()
                        title = (getattr(props, "title", "") or "").strip()
                    except Exception:  # pragma: no cover - COM boundary churn
                        logger.debug("media properties unavailable for a session")
                    out.append(
                        SessionInfo(
                            source_id=str(session.source_app_user_model_id),
                            status=STATUS_NAMES.get(int(info.playback_status), "Unknown"),
                            title=title,
                        )
                    )
                return out
            except Exception as exc:  # pragma: no cover - COM boundary churn
                last_exc = exc
                logger.warning("get_sessions failed (%s); retrying with a fresh manager", exc)
        raise last_exc  # type: ignore[misc]

    async def _find_session(self, manager, source_id: str, title: str):
        """The session to control for *source_id* — and, when known, the one
        whose media title matches *title*.

        Several sessions can share the same source id (one per tab,
        observed with Brave).  A lone session is controlled by identity
        (it was already title-matched when picked); with several, only a
        title-matching session is — anything else is refused rather than
        risk controlling an unrelated tab's media.
        """
        matches = [
            session
            for session in manager.get_sessions()
            if str(session.source_app_user_model_id) == source_id
        ]
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        for session in matches:
            try:
                props = await session.try_get_media_properties_async()
            except Exception:  # pragma: no cover - COM boundary churn
                continue
            session_title = (getattr(props, "title", "") or "").strip()
            if title and session_title and _title_matches(title, session_title):
                return session
        return None

    async def pause(self, source_id: str, title: str = "") -> bool:
        last_exc: Exception | None = None
        for fresh in (False, True):
            try:
                manager = await self._get_manager(fresh=fresh)
                session = await self._find_session(manager, source_id, title)
                if session is None:
                    return False
                return bool(await session.try_pause_async())
            except Exception as exc:  # pragma: no cover - COM boundary churn
                last_exc = exc
                logger.warning("pause failed (%s); retrying with a fresh manager", exc)
        raise last_exc  # type: ignore[misc]

    async def play(self, source_id: str, title: str = "") -> bool:
        last_exc: Exception | None = None
        for fresh in (False, True):
            try:
                manager = await self._get_manager(fresh=fresh)
                session = await self._find_session(manager, source_id, title)
                if session is None:
                    return False
                return bool(await session.try_play_async())
            except Exception as exc:  # pragma: no cover - COM boundary churn
                last_exc = exc
                logger.warning("play failed (%s); retrying with a fresh manager", exc)
        raise last_exc  # type: ignore[misc]


def _winrt_available() -> bool:
    try:
        importlib.import_module("winrt.windows.media.control")
        return True
    except Exception:
        return False


class MediaController(QtCore.QObject):
    """Monitors and pauses the relevant Windows media session, off-thread.

    Signals (all delivered on the GUI thread via queued connections):

    * ``tracked_state(str, str)`` — (source_id, status) of the tracked
      session whenever its status changes
    * ``session_lost(str)``       — the tracked session disappeared
    * ``pause_result(str, bool, str)`` — (source_id, ok, human message)
    """

    tracked_state = QtCore.Signal(str, str)
    session_lost = QtCore.Signal(str)
    pause_result = QtCore.Signal(str, bool, str)
    #: result of a user-initiated pause/resume of the selected session
    control_result = QtCore.Signal(str, bool, str)

    def __init__(
        self,
        parent: QtCore.QObject | None = None,
        backend_factory=_WinRtBackend,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory
        self._poll_interval_s = poll_interval_s
        self._available = _winrt_available()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pending_tasks: set = set()  # futures of all scheduled tasks
        self._hint: str | None = None
        self._title_query: str | None = None
        self._tracked_id: str | None = None
        self._last_status: str | None = None
        self._force_reemit = False  # consumed by the monitor thread itself
        self._monitoring = False

    # --- lifecycle ----------------------------------------------------------

    @property
    def available(self) -> bool:
        """Whether the Windows media-session API is usable at all."""
        return self._available

    def _ensure_thread(self) -> None:
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="media-control", daemon=True
        )
        self._thread.start()

    def _shutdown(self) -> None:
        self._monitoring = False
        if self._loop is not None:
            # Cancel every in-flight task (a poll mid-sleep, a control
            # command) so stopping the loop never leaves a destroyed-but-
            # pending task behind.
            pending = list(self._pending_tasks)
            self._pending_tasks.clear()

            def _cancel_and_stop() -> None:
                for future in pending:
                    future.cancel()
                # Next iteration: the cancelled tasks finalize first, then
                # the loop stops without destroying a pending task.
                self._loop.call_soon(self._loop.stop)

            self._loop.call_soon_threadsafe(_cancel_and_stop)
            if self._thread is not None:
                self._thread.join(timeout=3)
        self._loop = None
        self._thread = None

    def _schedule(self, coro) -> None:
        self._ensure_thread()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        self._pending_tasks.add(future)
        future.add_done_callback(self._pending_tasks.discard)

    # --- monitoring (state synchronization) ---------------------------------

    def start_monitoring(self, hint: str | None = None, title_query: str | None = None) -> None:
        """Poll the relevant session and report status changes.

        *hint* narrows candidates by app; *title_query* (the requested
        song) additionally restricts them to the session actually playing
        that song, so unrelated media is never tracked or controlled.
        """
        if not self._available:
            logger.info("Media monitoring unavailable (winrt missing)")
            return
        self._hint = hint
        self._title_query = title_query
        self._tracked_id = None
        self._last_status = None
        self._force_reemit = True  # report the current status once, soon
        if not self._monitoring:
            self._monitoring = True
            self._schedule(self._monitor_loop())

    def stop_monitoring(self) -> None:
        self._monitoring = False
        self._tracked_id = None
        self._last_status = None

    def recheck(self) -> None:
        """Make the next poll re-emit the current status even if unchanged.

        Used when the countdown was just gated on playback confirmation: a
        session that had *already* reached Playing would otherwise never
        emit again and the countdown would stay gated forever.

        The request is a flag consumed by the monitor thread itself — this
        method must not reset the shared last-status bookkeeping from the
        GUI thread, which races with the monitor's own emission decisions
        and can produce a duplicate Playing report (observed as a stale
        queued emission resuming a just-paused countdown).
        """
        self._force_reemit = True

    async def _monitor_loop(self) -> None:
        backend = self._backend_factory()
        while self._monitoring:
            try:
                infos = await backend.get_sessions()
                if not self._monitoring:
                    break
                tracked = pick_session(infos, self._hint, self._tracked_id, self._title_query)
                if tracked is None:
                    if self._tracked_id is not None:
                        self.session_lost.emit(self._tracked_id)
                        self._tracked_id = None
                        self._last_status = None
                else:
                    self._tracked_id = tracked.source_id
                    # Consume the force flag every tracked poll — a natural
                    # status change must not leave it armed for the next
                    # poll, which would duplicate the report.
                    reemit, self._force_reemit = self._force_reemit, False
                    if tracked.status != self._last_status or reemit:
                        self._last_status = tracked.status
                        self.tracked_state.emit(tracked.source_id, tracked.status)
            except Exception:  # pragma: no cover - defensive around COM boundaries
                logger.exception("Media monitoring iteration failed")
            await asyncio.sleep(self._poll_interval_s)

    # --- pause ----------------------------------------------------------------

    def pause_relevant(self, hint: str | None = None, title_query: str | None = None) -> None:
        """One-shot: pause the relevant session, report via pause_result."""
        if not self._available:
            self.pause_result.emit(
                "", False, "Windows media control is unavailable on this system."
            )
            return
        self._schedule(self._pause_relevant(hint, title_query))

    async def _pause_relevant(self, hint: str | None, title_query: str | None = None) -> None:
        backend = self._backend_factory()
        try:
            infos = await backend.get_sessions()
            target = pick_session(infos, hint, title_query=title_query)
            if target is None:
                if title_query:
                    message = (
                        f"No media session playing “{title_query}” was found — "
                        "pausing manually is safer."
                    )
                else:
                    message = "No unambiguous media session to pause — pausing manually is safer."
                self.pause_result.emit("", False, message)
                return
            ok = await backend.pause(target.source_id, target.title)
            if ok:
                self.pause_result.emit(
                    target.source_id, True, f"Paused the media session of {target.source_id}."
                )
            else:
                self.pause_result.emit(
                    target.source_id, False, f"{target.source_id} refused the pause command."
                )
        except Exception as exc:  # pragma: no cover - defensive around COM boundaries
            logger.exception("Pause attempt failed")
            self.pause_result.emit("", False, f"Media pause failed: {exc}")

    # --- user pause/resume of the selected song ------------------------------

    def pause_selected(self, hint: str | None = None, title_query: str | None = None) -> None:
        """User pressed Pause: pause the selected song's session, nothing else.

        The tracked session is controlled by identity; only when nothing is
        tracked does hint/title matching apply.  An unrelated session is
        never a fallback target — ambiguity and absence are honest failures.
        """
        if not self._available:
            self.control_result.emit(
                "", False, "Windows media control is unavailable — only the countdown was paused."
            )
            return
        self._schedule(self._control_selected(True, hint, title_query))

    def resume_selected(self, hint: str | None = None, title_query: str | None = None) -> None:
        """User pressed Resume: resume the selected song's session, nothing else."""
        if not self._available:
            self.control_result.emit(
                "", False, "Windows media control is unavailable — the countdown stays paused."
            )
            return
        self._schedule(self._control_selected(False, hint, title_query))

    async def _control_selected(
        self, pause: bool, hint: str | None, title_query: str | None
    ) -> None:
        backend = self._backend_factory()
        try:
            infos = await backend.get_sessions()
            target = None
            if self._tracked_id is not None:
                # The tracked id alone is not enough: an app can reuse its
                # single session for unrelated media (observed with Brave),
                # so the title is re-validated before anything is touched.
                target = pick_session(infos, None, tracked_id=self._tracked_id, title_query=title_query)
            if target is None:
                target = pick_session(infos, hint, title_query=title_query)
            if target is None:
                self.control_result.emit(
                    "",
                    False,
                    "The selected song has no controllable media session — nothing else was touched."
                    if pause
                    else "The selected song has no controllable media session — the countdown stays paused.",
                )
                return
            if pause:
                ok = await backend.pause(target.source_id, target.title)
            else:
                ok = await backend.play(target.source_id, target.title)
            if ok:
                self.control_result.emit(
                    target.source_id,
                    True,
                    "Playback paused — the countdown is paused too."
                    if pause
                    else "Playback resuming — the countdown continues once it is confirmed playing.",
                )
            else:
                self.control_result.emit(
                    target.source_id,
                    False,
                    f"{target.source_id} refused the {'pause' if pause else 'resume'} command — "
                    + ("the song may still be playing." if pause else "the countdown stays paused."),
                )
        except Exception as exc:  # pragma: no cover - defensive around COM boundaries
            logger.exception("Selected-session control failed")
            self.control_result.emit("", False, f"Media control failed: {exc}")