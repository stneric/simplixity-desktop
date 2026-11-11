# Simplixity Desktop

[Simplixity](https://github.com/stneric/simplixity), the research assistant
that answers with checked citations, as a desktop app for macOS, Windows and
Linux. No Docker, no Redis server, no separate SearXNG service: everything runs
in one process. You bring the API keys (OpenRouter, or a local model through
Ollama or LM Studio). Your chats stay on your computer.

## Install

Download the installer for your system from
[Releases](https://github.com/stneric/simplixity-desktop/releases):

| System | File |
|---|---|
| macOS (Apple Silicon) | `.dmg` |
| Windows | `-setup.exe` |
| Linux | `.AppImage` |

The builds are not signed, so the system warns you once:

- **macOS:** open the app, confirm the warning, then System Settings ->
  Privacy & Security -> "Open Anyway". Or in a terminal:
  `xattr -dr com.apple.quarantine /Applications/Simplixity.app`
- **Windows:** SmartScreen -> "More info" -> "Run anyway".
- **Linux:** `chmod +x Simplixity-*.AppImage`, then run it. It needs the Qt
  WebEngine system libraries (`libnss3`, `libxkbcommon-x11-0`, `libxcb-cursor0`, ...).

On the first start the app asks for your API keys. After that it updates
itself from the Releases page.

## Your data

Chats, settings and API keys live in one folder:

| System | Folder |
|---|---|
| macOS | `~/Library/Application Support/Simplixity Desktop/` |
| Windows | `%APPDATA%\Simplixity Desktop\` |
| Linux | `~/.local/share/simplixity-desktop/` |

API keys are stored in `.env` there and are sent only to the service each key
belongs to. The log is `desktop.log` in the same folder.

The app's server listens on `127.0.0.1` only and refuses requests from other
websites (it checks `Host`, `Origin` and `Sec-Fetch-Site`), so a page open in
your browser cannot use it. Other programs running under your user account
can reach it, the same way they can read your files.

## Updates

Installed apps check the latest release at start and every 6 hours and
install it on a click. Each download must match the SHA-256 GitHub reports
for it. That catches broken downloads, not a compromised GitHub account:
whoever can publish a release here can ship code to every install.

## How it works

This repo is the shell around Simplixity. It does not copy Simplixity or
SearXNG: `fetch.py` checks them out at the versions pinned in
[`upstream.toml`](upstream.toml), so a new upstream commit changes nothing
until the pin is bumped.

| Docker setup | Desktop app |
|---|---|
| `redis` container | `fakeredis` in the process (running jobs are lost on quit, chats stay) |
| `searxng` container | SearXNG on a thread, random loopback port |
| `simplixity` container | uvicorn thread on `127.0.0.1:3210` |
| browser | pywebview: WKWebView (macOS), WebView2 (Windows), Qt WebEngine (Linux) |

Simplixity's pages are served unmodified. `skin.py` adds `skin/` on top for
the desktop look: native title bar and sidebar on macOS, no text selection in
controls, a trimmed right-click menu. The skin only overrides styles, so if
upstream renames something, that spot falls back to the browser look instead
of breaking. The selectors it relies on are listed at the top of
`skin/skin.css`.

There are no user profiles. The app signs into a single local one.

## Development

```sh
uv sync --group dev
uv run python fetch.py
uv run python simplixity_desktop.py
uv run pytest
```

Environment variables:

| Variable | Effect |
|---|---|
| `SIMPLIXITY_DESKTOP_DATA` | data folder |
| `SIMPLIXITY_DESKTOP_PORT` | server port |
| `SIMPLIXITY_DESKTOP_HEADLESS=1` | no window; open the port in a browser instead |
| `SIMPLIXITY_SRC=/path/to/simplixity` | run against a local Simplixity checkout |

To move to a newer Simplixity or SearXNG, change `ref` in `upstream.toml`,
run `fetch.py` (it reports dependencies missing from `pyproject.toml`), and
click through the app before committing.

### Building installers

```sh
uv sync --group build
uv run python fetch.py
uv run python build.py
```

`build.py` builds for the system it runs on; PyInstaller does not
cross-compile. macOS needs the Xcode command line tools, Windows needs Inno
Setup 6. The GitHub Actions workflow builds all three.

push to start build for 0.2.2

## License

AGPL-3.0, like Simplixity and SearXNG.
