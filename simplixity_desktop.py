"""Simplixity as a desktop app (macOS, Windows, Linux), in a single process.

What the compose stack runs as three containers runs here as one Python process:

- Redis is replaced by fakeredis, an in-memory implementation of the same API
  (streams and blocking XREAD included). Live run state is lost when the window
  closes; chats, projects and settings live in SQLite and are kept.
- SearXNG is imported from its source tree (./.searxng) and served by a thread
  on a random loopback port. Its settings are read from Simplixity's
  docker-compose.yml, so the engine list has one source of truth.
- The FastAPI app runs in a uvicorn thread on 127.0.0.1, and pywebview shows it
  in a native web view (WKWebView, WebView2, Qt WebEngine) on the main thread
  (macOS requires that).

One instance per data directory: the app's port is taken first thing, and a
second start that finds it taken leaves without touching anything.

Simplixity itself is not part of this repo: fetch.py checks out the ref pinned
in upstream.toml into ./.simplixity, and nothing there is modified. The seams:

- `app.main.Redis`, which the lifespan uses to open its connection; it is
  replaced before the app starts.
- The ASGI app is wrapped by skin.py, which adds skin/ to every HTML page.
  All desktop look-and-feel lives there, as overrides on top of upstream.
- And by desktop_api.py: one profile signed in automatically, API keys
  entered in the app instead of an .env.

What runs can be newer than what was built: updater.py may point REPO,
SEARXNG_SRC and SKIN_DIR at an update (code and Python packages) installed
from a GitHub release into the data directory.

Run with:  uv run python simplixity_desktop.py
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
HERE = Path(__file__).resolve().parent
if FROZEN:
    # PyInstaller unpacks everything next to the executable; simplixity.spec lays
    # it out as <bundle>/app, <bundle>/web, <bundle>/searx, <bundle>/skin,
    # <bundle>/docker-compose.yml.
    BUNDLE = Path(sys._MEIPASS)
    REPO = BUNDLE
    SEARXNG_SRC = BUNDLE
    SKIN_DIR = BUNDLE / "skin"
else:
    REPO = Path(os.environ.get("SIMPLIXITY_SRC", HERE / ".simplixity")).resolve()
    SEARXNG_SRC = HERE / ".searxng"
    SKIN_DIR = HERE / "skin"

PLATFORM = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Simplixity Desktop"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Simplixity Desktop"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "simplixity-desktop"


DATA_DIR = Path(os.environ.get("SIMPLIXITY_DESKTOP_DATA", default_data_dir()))
# Fixed, not random: the web view keys localStorage by origin, port included,
# so a new port on every launch would forget the UI's local preferences.
APP_PORT = int(os.environ.get("SIMPLIXITY_DESKTOP_PORT", "3210"))

log = logging.getLogger("simplixity.desktop")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def claim_port() -> socket.socket | None:
    """Bind the app's port now, before anything else runs; None if it is taken.

    Held from here on and handed to uvicorn, so a second start cannot get
    between the check and the server. SO_REUSEADDR only where it means "past
    TIME_WAIT" (a quick restart): on Windows it lets a second process bind a
    port already in use, and without it Windows already allows the restart.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", APP_PORT))
    except OSError:
        sock.close()
        return None
    return sock


def ours_is_running() -> bool:
    """Whether what holds the port is this app (it has the desktop API)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/desktop/api/update", timeout=3) as resp:
            return resp.status == 200
    except OSError:
        return False


def wait_for(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not answer within {timeout:.0f}s")


# --- SearXNG ---------------------------------------------------------------


def write_searxng_settings(secret: str) -> Path:
    """Take the settings.yml the compose file inlines and write it to disk."""
    import yaml

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    content: str = compose["configs"]["searxng_settings"]["content"]
    # Compose interpolates "${SEARXNG_SECRET:?...}"; nothing does that here.
    content = re.sub(r"\$\{SEARXNG_SECRET[^}]*\}", secret, content)
    path = DATA_DIR / "searxng-settings.yml"
    path.write_text(content, encoding="utf-8")
    return path


def start_searxng() -> str:
    if not (SEARXNG_SRC / "searx").is_dir():
        sys.exit(f"SearXNG source not found at {SEARXNG_SRC}; run `python fetch.py` first.")

    secret = secrets.token_hex(32)
    os.environ["SEARXNG_SETTINGS_PATH"] = str(write_searxng_settings(secret))
    os.environ["SEARXNG_SECRET"] = secret
    if not FROZEN:
        sys.path.insert(0, str(SEARXNG_SRC))
    if sys.platform == "win32" and "pwd" not in sys.modules:
        # searx/valkeydb.py imports the Unix-only `pwd` at the top and uses it
        # only when a Valkey URL is configured, which it never is here.
        import types

        sys.modules["pwd"] = types.ModuleType("pwd")

    from werkzeug.serving import make_server

    import searx.webapp  # importing it loads the settings and initialises every engine

    # SearXNG reconfigures logging on import; take this module's level back.
    log.setLevel(logging.INFO)
    # Logged on every request that does not come through a reverse proxy, which
    # here is all of them; the limiter it belongs to is off.
    logging.getLogger("searx.botdetection").setLevel(logging.CRITICAL)

    port = free_port()
    server = make_server("127.0.0.1", port, searx.webapp.app, threaded=True)
    threading.Thread(target=server.serve_forever, name="searxng", daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    wait_for(f"{url}/healthz", timeout=30)
    log.info("SearXNG in-process at %s", url)
    return url


# --- Simplixity ------------------------------------------------------------


def configure_simplixity(searxng_url: str) -> None:
    """Settings are read at import time, so this runs before `import app`."""
    os.environ["SEARXNG_API_URL"] = searxng_url
    os.environ["DATABASE_PATH"] = str(DATA_DIR / "simplixity.db")
    # The app's Settings read ".env" relative to the working directory, so an
    # API key goes into DATA_DIR/.env. Variables set above still win over it.
    os.chdir(DATA_DIR)
    if not FROZEN:
        sys.path.insert(0, str(REPO))


def patch_redis() -> None:
    import fakeredis

    import app.main

    server = fakeredis.FakeServer()

    class InProcessRedis:
        @staticmethod
        def from_url(_url: str, decode_responses: bool = False, **_kw):
            return fakeredis.FakeAsyncRedis(server=server, decode_responses=decode_responses)

    app.main.Redis = InProcessRedis


def use_payload(path: Path) -> None:
    """Run Simplixity, SearXNG, the skin and their packages from an update.

    Prepended to sys.path (before anything third-party is imported), so `app`,
    `searx`, desktop_api, skin and every package in lib/ resolve there before
    the bundle's copies. What only the shell has (pywebview, AppKit) still
    comes from the bundle.
    """
    global REPO, SEARXNG_SRC, SKIN_DIR
    REPO = SEARXNG_SRC = path
    SKIN_DIR = path / "skin"
    sys.path[:0] = [str(path), str(path / "lib")]


def start_simplixity(updates, sock: socket.socket):
    import uvicorn

    import desktop_api
    import skin
    from app.config import settings
    from app.main import app as asgi_app  # an object, not "app.main:app": no import by string in a bundle

    config = uvicorn.Config(
        desktop_api.wrap(skin.wrap(asgi_app, SKIN_DIR, PLATFORM), asgi_app, DATA_DIR, updates),
        host="127.0.0.1",
        port=APP_PORT,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=3,
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="uvicorn", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{APP_PORT}"
    wait_for(f"{url}/api/health", timeout=30)
    return server, thread, url


# --- window ----------------------------------------------------------------

WINDOW_STATE = DATA_DIR / "window.json"


def load_geometry() -> dict:
    """Last size and position, if the position is still on a screen.

    Windows and Linux only. On macOS pywebview reports the frame but sets the
    content size, and measures y from a different edge than it moves by, so a
    round trip drifts; macos.py lets AppKit's frame autosave do it instead.
    """
    import json

    import webview

    geo = {"width": 1280, "height": 860}
    try:
        saved = json.loads(WINDOW_STATE.read_text(encoding="utf-8"))
        geo["width"], geo["height"] = max(int(saved["width"]), 420), max(int(saved["height"]), 600)
        x, y = int(saved["x"]), int(saved["y"])
        if any(s.x <= x < s.x + s.width - 100 and s.y <= y < s.y + s.height - 100 for s in webview.screens):
            geo["x"], geo["y"] = x, y
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return geo


def save_geometry(window) -> None:
    import json

    try:
        WINDOW_STATE.write_text(json.dumps(
            {"x": window.x, "y": window.y, "width": window.width, "height": window.height}
        ), encoding="utf-8")
    except Exception:  # noqa: BLE001 - losing the window size is not worth a crash on quit
        log.exception("could not save window geometry")


def open_window(url: str) -> None:
    import webview

    window = webview.create_window(
        "Simplixity",
        url,
        min_size=(420, 600),
        # Off is pywebview's default, which makes an answer impossible to copy.
        # The skin turns selection off again where an app has none: on chrome.
        text_select=True,
        **({"width": 1280, "height": 860} if sys.platform == "darwin" else load_geometry()),
    )
    if sys.platform != "darwin":
        window.events.closing += lambda: save_geometry(window)
    else:
        import macos  # importing it installs the drag and context-menu handling

        window.events.before_show += lambda: macos.setup_window(window)
    # private_mode=False keeps cookies and localStorage between launches.
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Before the log file: a second start must not empty the running one's log.
    sock = claim_port()
    if sock is None:
        why = ("Simplixity is already running" if ours_is_running()
               else f"port {APP_PORT} is in use by another program (set SIMPLIXITY_DESKTOP_PORT)")
        if FROZEN:
            with open(DATA_DIR / "desktop.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} not started: {why}\n")
        sys.exit(f"Not started: {why}.")
    if FROZEN:
        # A windowed bundle has no console (on Windows sys.stdout is None, which
        # uvicorn's logging trips over), so everything goes to a file instead.
        logfile = open(DATA_DIR / "desktop.log", "w", buffering=1, encoding="utf-8")  # noqa: SIM115
        sys.stdout = sys.stderr = logfile
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    log.info("data in %s", DATA_DIR)
    if not FROZEN and not (REPO / "app").is_dir():
        sys.exit(f"Simplixity source not found at {REPO}; run `python fetch.py` first.")

    import updater

    updates = updater.Store(
        DATA_DIR, updater.shell_info(BUNDLE if FROZEN else HERE, FROZEN), updater.enabled_here(FROZEN)
    )
    payload = updates.boot()
    if payload:
        use_payload(payload)

    searxng_url = start_searxng()
    configure_simplixity(searxng_url)
    patch_redis()
    server, thread, url = start_simplixity(updates, sock)
    updates.confirm()
    updates.check_in_background()

    if os.environ.get("SIMPLIXITY_DESKTOP_HEADLESS", "").lower() in ("1", "true", "yes"):
        log.info("headless: %s (Ctrl-C to stop)", url)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
    else:
        open_window(url)

    server.should_exit = True
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
