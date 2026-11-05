# Simplixity Desktop

[Simplixity](https://github.com/stneric/simplixity), the research assistant
that answers with checked citations, as a desktop app for macOS, Windows and
Linux. It runs in a single Python process: no Docker, no Redis server, no
separate SearXNG service. You bring the API keys (OpenRouter, or a local
model through Ollama or LM Studio); your chats stay on your computer.

## Download

Installers are on the [Releases](https://github.com/stneric/simplixity-desktop/releases)
page: `.dmg` for macOS (Apple Silicon), `-setup.exe` for Windows, `.AppImage`
for Linux. The app updates itself from there afterwards.

The builds are not signed, so the system asks once:

- **macOS:** open the app, confirm the warning, then System Settings ->
  Privacy & Security -> "Open Anyway". Or, in a terminal:
  `xattr -dr com.apple.quarantine /Applications/Simplixity.app`
- **Windows:** SmartScreen -> "More info" -> "Run anyway".
- **Linux:** `chmod +x Simplixity-*.AppImage`, then run it. It needs the Qt
  WebEngine system libraries (`libnss3`, `libxkbcommon-x11-0`, `libxcb-cursor0`, ...).

On the first start, Settings -> API keys opens by itself.

## How it works

| Compose stack | Here |
|---|---|
| `redis` container | `fakeredis` inside the process (run state is lost on quit; chats stay) |
| `searxng` container | SearXNG source, served by a thread on a random loopback port |
| `simplixity` container | uvicorn thread on `127.0.0.1:3210` |
| browser | pywebview: WKWebView (macOS), WebView2 (Windows), Qt WebEngine (Linux) |

## What is in this repo, and what is not

This repo is only the shell around Simplixity. Simplixity itself and SearXNG
are **not** copied in: `fetch.py` checks them out into `.simplixity/` and
`.searxng/` at the refs pinned in [`upstream.toml`](upstream.toml).

| File | Job |
|---|---|
| `upstream.toml` | which Simplixity and SearXNG the app is built from |
| `fetch.py` | checks those out; fails if Simplixity needs a dependency `pyproject.toml` lacks |
| `simplixity_desktop.py` | the launcher: SearXNG thread, fakeredis, uvicorn, window |
| `skin.py` + `skin/` | the desktop look, layered over Simplixity's unmodified pages |
| `desktop_api.py` | one user: signs into a single profile silently; API keys entered in Settings |
| `macos.py` | macOS window chrome: unified title bar, native drag, context menu |
| `updater.py` | in-app updates from GitHub releases, without a new installer |
| `build.py`, `simplixity.spec` | installers |

Because of the pin, a new commit in Simplixity changes nothing here until
`upstream.toml` is bumped on purpose.

### The skin

Simplixity's frontend is served as-is. `skin.py` wraps the ASGI app and adds
`skin/skin.css`, `skin/<platform>.css` and `skin/skin.js` to every HTML page,
after Simplixity's own stylesheet. The skin only *overrides*: if Simplixity
renames something the skin targets, that rule stops matching and the app
looks like the browser version there, it does not break. The selectors the
skin relies on are listed at the top of `skin/skin.css`.

What it does today:

- Text can be selected in content, not in chrome. Controls get the arrow
  cursor, and the window as a whole does not rubber-band scroll.
- macOS: the topbar is the title bar (transparent, 52px, window buttons on
  its centre line), and the sidebar is translucent over the native sidebar
  material. Empty topbar space drags the window and double-click zooms it,
  natively, so snapping and the system setting apply. The right-click menu
  keeps Copy / Look Up / Share and drops Reload / Back / Inspect. Scroll bars
  are the overlay kind even with a mouse attached (for this app only).
- No service worker: `/sw.js` answers with one that removes itself, so an
  offline shell from the browser build cannot outlive an app update.
- Window size and position are remembered.

### Local server

The app's server listens on `127.0.0.1` only and signs every request into
your profile. So that other web pages open in your browser cannot use it,
it answers only requests whose `Host` is its own address and that do not come
from another site (`Origin`, `Sec-Fetch-Site`). This blocks DNS rebinding and
cross-site form posts. Other programs running as you on the same computer can
still reach it, as they can read your files anyway.

Only one instance runs per data directory: a second start finds the port
taken and quits.

## Run from source

```sh
uv sync --group dev
uv run python fetch.py
uv run python simplixity_desktop.py
uv run pytest
```

Data lives in the data directory listed under Installers below. API keys
are entered in the app (Settings -> API keys, opened by itself on the first
start without an OpenRouter key); they are stored in `.env` there, which can
also be edited by hand (same format as Simplixity's `.env.example`).

There are no profiles in the app: it signs into one silently (the one that
already exists, or `me` on a fresh install; remembered in `desktop.json`).

Environment overrides: `SIMPLIXITY_DESKTOP_DATA`, `SIMPLIXITY_DESKTOP_PORT`,
`SIMPLIXITY_DESKTOP_HEADLESS=1` (no window, serve until Ctrl-C; the skin is
served too, so a browser on that port shows it), `SIMPLIXITY_SRC=/path/to/simplixity`
(run against a local checkout instead of the pinned one, e.g. to try a branch).

## Updating Simplixity or SearXNG

1. Change `ref` in `upstream.toml` (a tag or a full commit SHA).
2. `uv run python fetch.py`. If it reports missing dependencies, add them to
   `pyproject.toml` and run `uv lock`.
3. Run the app and click through it, the selectors listed in `skin/skin.css`
   in particular.
4. Commit `upstream.toml` (and `pyproject.toml`/`uv.lock` if they changed).

## App updates without a new installer

The installed app is a small fixed shell (Python with its whole standard
library, pywebview, the launcher, `updater.py`, `macos.py`). Everything else
(Simplixity, SearXNG, the skin, `desktop_api.py`, `skin.py` and the Python
packages from `uv.lock`) is the *update payload*.

The download must match the SHA-256 GitHub reports for the release asset.
That catches broken or swapped downloads, not a hijacked repository: whoever
can publish a release there can ship code to every install. 

## Installers

```sh
uv sync --group build
uv run python fetch.py
uv run python build.py      # builds for the OS it runs on
```

| OS | Output in `dist/` | Needs |
|---|---|---|
| macOS | `Simplixity-<version>-macos-<arch>.dmg` | Xcode command line tools |
| Windows | `Simplixity-<version>-windows-x64-setup.exe` | Inno Setup 6 |
| Linux | `Simplixity-<version>-linux-<arch>.AppImage` | Qt WebEngine system libs; appimagetool is downloaded |

`<version>` is the version of the Simplixity it is built from.

PyInstaller does not cross-compile.
The builds are not signed; see Download above for what that means on each system.

A bundled app writes its log to `desktop.log` in the data directory:
`~/Library/Application Support/Simplixity Desktop/` on macOS,
`%APPDATA%\Simplixity Desktop\` on Windows, `~/.local/share/simplixity-desktop/` on Linux.

## License

AGPL-3.0, like Simplixity and SearXNG.
