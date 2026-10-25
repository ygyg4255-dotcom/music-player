# Dynamic Island Music Player

A compact, always-on-top floating music timer for Windows — inspired by
Apple's Dynamic Island. Enter a song and a duration; the app finds the song
in your Music folder (or falls back to YouTube in your existing Brave
browser), plays it, and pauses the playback when the timer expires.

Built with **Python 3.13** and **PySide6**.

## What it does

1. **Local-first search** — looks for the song in your Windows *Music*
   known folder (resolved via the Windows known-folder API, so OneDrive
   redirection works). Exact filename matches are preferred; close
   matches are found with RapidFuzz fuzzy matching; weak matches are
   rejected (the minimum score is 62/100, configurable in `config.py`).
2. **Default-app playback** — the match opens with whatever application
   you have associated with that file type. Nothing is re-associated.
3. **YouTube fallback** — no local match? The app fetches the YouTube
   search results itself, picks the first result whose title matches the
   song, and opens that result's *watch page* as a new tab in your
   *existing* Brave browser session (Brave is located via the filesystem
   and the registry; if absent, the system default browser is used). No
   browser automation, remote debugging (CDP), or Brave profile/security
   changes are ever involved — the browser is only handed a URL, and it
   is never closed by the app. If the first result cannot be determined
   (offline, consent wall), the search page opens instead.
4. **Countdown timer** — a monotonic-clock countdown (0.1–10,080 min,
   i.e. up to 7 days) with pause / resume / cancel, drift-free because
   the remaining time is always computed, never decremented.
5. **Playback synchronization** — the countdown never consumes duration
   during startup: from the moment **Start** is pressed it stays paused —
   with the full duration intact — until the selected song's media session
   (GSMTC) is *confirmed playing* (local player or YouTube tab alike).
   Once running, the session is monitored: if you manually pause the
   music, the countdown pauses too, and resumes from the remaining time
   when you resume playback. A pause we triggered ourselves (timer
   expiry) never re-enters that path, so there are no feedback loops.
   Only the media session whose title matches the requested song is ever
   tracked, resumed, or paused — and the title is re-validated on every
   poll, so a session that switches to unrelated media (e.g. a browser
   tab switch) is dropped, never controlled by mistake.
6. **User pause/resume** — the island **⏸** pauses the countdown
   immediately (exact remaining time) *and* pauses the selected song's
   media session; **▶** resumes that song, and the countdown continues
   only once the session is confirmed *Playing*, from the exact
   remaining time. If the selected session is gone or uncontrollable,
   the app reports it honestly and touches nothing else.
7. **Expiry pause** — when the timer hits zero, the app pauses the
   playback it started via the Windows media-session API. If the app
   started nothing, it pauses the *unique playing* media session; with
   several ambiguous sessions it refuses rather than pausing the wrong
   one and tells you why.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
```

**Development environment requirements**

- Windows 10/11 (the media-control layer uses Windows-only APIs)
- Python 3.13 (other 3.10+ versions likely work; untested)
- PySide6 6.8+, RapidFuzz 3.9+, the `winrt-*` projection packages
  (installed automatically from `requirements.txt`)
- PyInstaller 6.10+ (only for packaging)

## Launch

From source:

```powershell
python main.py            # normal launch
python main.py --smoke    # automated check: window opens, closes after 4 s
pytest                    # run the test suite (170 tests)
```

## Usage

- The pill floats top-center and always stays on top; drag it anywhere.
- Click **▾** to expand: enter a song name, a duration in minutes, press
  **Start** (or press Enter in the song field).
- The pill shows the remaining time; **⏸** pauses/resumes the countdown
  *and the selected song's playback* (the countdown resumes only once
  the song is confirmed playing again), **✕** cancels it. After expiry,
  **✓** dismisses the "Time's up" state.
- Song name, duration, and the expanded/collapsed state are remembered
  between launches (JSON settings with atomic writes and a `.bak`
  recovery copy under `%LOCALAPPDATA%\dynamic-island-music-player\`).
- **Start with Windows** (expanded panel) is an optional opt-in. It is
  off by default; ticking it writes exactly one value under your
  `HKCU\...\CurrentVersion\Run` key, and unticking it removes only that
  value. No services, no scheduled tasks, no machine-wide changes.
- Closing the window exits the process — no tray icon, no background
  process is left behind, and no other application (Brave, your player)
  is ever closed by the app.

## Dependencies

| Package | Purpose |
|---|---|
| PySide6 | the floating Qt interface |
| rapidfuzz | fuzzy song-title matching |
| winrt-runtime + winrt-Windows.Media.Control / -Foundation / -Foundation.Collections | Windows GSMTC media-session control |
| pytest, pyinstaller | development only |

## Troubleshooting

- **"No local match for …"** — the song is not in your Music folder (or
  scored below the fuzzy threshold). The YouTube fallback then opens.
- **"Time's up — … cannot be paused automatically"** — the playback
  source exposes no controllable Windows media session, and the system
  media-session API found nothing unambiguous to pause. Some apps
  (games, certain browsers configurations) expose no session at all.
- **"No unambiguous media session to pause"** — several apps are playing
  audio; the app refuses to guess. Pause manually.
- **Settings problems** — the settings file is
  `%LOCALAPPDATA%\dynamic-island-music-player\settings.json`; if it is
  corrupt, the previous good copy (`settings.json.bak`) is used, and if
  that is also corrupt, safe defaults apply.
- **Logs** — every run appends to
  `%LOCALAPPDATA%\dynamic-island-music-player\music-player.log`
  (important for the packaged exe, which has no console).
- **UWP media players report state with lag** — the app polls the media
  session every 2 s and re-verifies transitions, so pause/resume
  detection can trail the actual button press by up to a couple of
  seconds.

## Known limitations (verified, not assumed)

- **YouTube autoplay depends on the browser.** The first suitable result's
  watch page opens automatically in your existing Brave session (the app
  determines the result by fetching and parsing the search page itself —
  no CDP, no remote debugging, no profile or security changes), but
  whether that page *starts playing* is the browser's and YouTube's
  decision (autoplay policy / Media Engagement Index). Until the matching
  media session reports *Playing*, the countdown stays paused with the
  full duration intact and the status line explains why; if the page does
  not autoplay, one manual click on play is needed. Verified live: Brave
  autoplays the opened watch page in the user's long-running browser
  session on this machine; a freshly launched browser may block autoplay
  until it has been interacted with, in which case the countdown simply
  keeps waiting.
- **Brave's media session appears only once a tab is actually playing.**
  Until then there is nothing to synchronize or pause, which is inherent
  to the Windows media-session API.
- **Media-session control depends on the player.** PotPlayer, Microsoft
  Media Player, and Brave expose sessions while playing (pause verified
  live against PotPlayer and Microsoft Media Player); other sources
  (games, some streaming apps) may expose nothing. The app reports this
  honestly instead of pausing blindly.
- **One relevant session at a time.** With multiple concurrent media
  sessions the app only acts when the target is unambiguous. Apps can
  even reuse a single session for different media (observed with Brave
  switching to another tab's video): the tracked session's title is
  re-validated on every poll, and a session that no longer plays the
  selected song is dropped — reported as lost, never paused or resumed
  by mistake.
- **The floating window's visual appearance (rounded corners, shadows,
  animation smoothness) is visually unverified** — the specification
  forbids screenshot-based verification. Window properties, sizes,
  flags, and layout behavior are covered by automated tests instead.

## Packaging (PyInstaller)

```powershell
.\.venv\Scripts\Activate.ps1
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name DynamicIslandMusicPlayer --paths src main.py
```

The executable lands in `dist\DynamicIslandMusicPlayer.exe` (the same
command also writes/updates the reproducible `DynamicIslandMusicPlayer.spec`
at the project root). Verify it with:

```powershell
.\dist\DynamicIslandMusicPlayer.exe --smoke
```

(the window appears for ~4 s and the process exits with code 0), and
check `%LOCALAPPDATA%\dynamic-island-music-player\music-player.log` for
the "Windows media-session control available: True" line, which proves
the GSMTC layer survived packaging.

## Architecture

```
main.py                     entry point (--smoke for automated checks)
src/music_player/
  app.py                    application wiring: lifecycle, sync, expiry
  config.py                 constants, thresholds, file locations
  settings.py               JSON settings persistence (atomic, .bak recovery)
  theme.py                  visual theme (colors, stylesheet)
  island.py                 the floating "Dynamic Island" widget (PySide6)
  timer.py                  monotonic-clock countdown component
  local_search.py           Music-folder search + fuzzy matching
  playback/
    base.py                 PlaybackSource interface + capability reporting
    local.py                default-app launch + default-player detection
    orchestrator.py         local→fallback pipeline (background search)
  media_control.py          Windows GSMTC media-session control
  youtube.py                YouTube fallback: first result + Brave integration
  startup.py                optional HKCU Run registration (opt-in)
tests/                      pytest suite (170 tests)
scripts/
  smoke_launch.py           quick launch/exit check
  probe_media_sessions.py   list current Windows media sessions
  probe_media_control_live.py  live pause/resume check against a player
  probe_brave_live.py       live Brave/YouTube fallback check
  probe_app_live.py         full live workflow check (local file)
  probe_youtube_live.py     live YouTube fallback component check
  probe_youtube_app_live.py full live workflow check (YouTube fallback)
```

## Verification summary

- 170 automated tests, all passing (`pytest`).
- Live, non-image verification performed during development:
  local search against the real Music folder; GSMTC pause/resume against
  PotPlayer and Microsoft Media Player; Brave tab opening + same-session
  reuse + preservation; the full local workflow (search → countdown held
  from Start → full duration once confirmed playing → island ⏸ pauses
  both the countdown at the exact remaining time and only the selected
  song's session, with unrelated playing sessions untouched → ▶ resumes
  song then countdown from the exact remaining time → expire → pause →
  clean exit) via `scripts\probe_app_live.py`; and the full YouTube
  workflow (no local match → first result's watch page opened in the
  existing Brave session → countdown held until playback confirmed →
  island ⏸/▶ controlling only the selected song's tab → sync pause/resume
  → expiry pause of the selected song → Brave preserved) via
  `scripts\probe_youtube_app_live.py`. The app's honest refusals were
  also verified live: with no matching session it keeps the countdown
  paused with the full duration and never falls back to pausing an
  unrelated session.