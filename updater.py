"""App updates from GitHub releases, without a new installer.

The installed app is a small fixed shell: Python, pywebview, the launcher,
macos.py and this file. Everything else is the *payload*: Simplixity, SearXNG,
the skin, desktop_api.py, skin.py, and the Python packages they need (lib/).
Every release carries one payload per platform next to the installers
(build.py makes them, the Desktop workflow uploads them):

    Simplixity-payload-macos-arm64.json   {"version", "serial", "shell"}
    Simplixity-payload-macos-arm64.zip    app/ web/ searx/ skin/ lib/ ...

The app looks for a newer release at start and every few hours, says so in
the window, and installs it when the user clicks. It is used from the next
start on, from the data directory:

    <data dir>/payload/state.json         which one is active, which came before
    <data dir>/payload/<serial>/          one installed payload

A payload fits only the shell it was built with (`shell`: launcher, this file,
macos.py, the spec, Python version, platform). When the newest release needs
a new shell, the app links to its installer instead.

Checks: the downloaded files must match the SHA-256 GitHub reports for the
release assets, and the ZIP's entries must stay inside their folder (no
absolute paths, no `..`, no links, no more than MAX_UNPACKED). That catches broken or swapped downloads.
It does not stop someone who controls the GitHub repository: they can publish
a release, and the app will offer it. Keep that account safe (2FA).

A payload that does not bring the server up is dropped on the next start.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

log = logging.getLogger("simplixity.desktop")

GITHUB_REPO = "stneric/simplixity-desktop"
LATEST_RELEASE = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CHECK_EVERY = 6 * 3600  # seconds, while the app is open

# What the shell is made of; a payload fits a shell whose fingerprint it names.
# Line endings are normalised: Windows checkouts may have CRLF.
SHELL_FILES = ("simplixity_desktop.py", "updater.py", "macos.py", "simplixity.spec")

PAYLOAD_TOP = {"app", "web", "searx", "skin", "lib", "docker-compose.yml", "desktop_api.py", "skin.py"}
PAYLOAD_REQUIRED = {"app", "web", "searx", "skin", "lib", "docker-compose.yml"}
MAX_MANIFEST = 64 * 1024
MAX_ZIP = 1024 * 1024 * 1024
MAX_UNPACKED = 4 * 1024 * 1024 * 1024  # what the entries may add up to once extracted


class UpdateError(Exception):
    """Shown to the user as is."""


# --- shell -----------------------------------------------------------------


def platform_tag() -> str:
    """macos-arm64, windows-x64, linux-x86_64: also in the installer names."""
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "windows-x64" if machine in ("amd64", "x86_64") else f"windows-{machine}"
    return f"{'macos' if sys.platform == 'darwin' else 'linux'}-{machine}"


def payload_name() -> str:
    return f"Simplixity-payload-{platform_tag()}"


def fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    for name in SHELL_FILES:
        data = (root / name).read_bytes().replace(b"\r\n", b"\n")
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    h.update(f"python {sys.version_info[0]}.{sys.version_info[1]} {platform_tag()}".encode())
    return h.hexdigest()


def shell_info(root: Path, frozen: bool) -> dict:
    """{"shell", "serial", "version"} of what was installed.

    A bundle reads it from shell.json (build.py). From source the fingerprint
    is computed and the built-in code counts as oldest.
    """
    if frozen:
        try:
            info = json.loads((root / "shell.json").read_text(encoding="utf-8"))
            return {"shell": str(info["shell"]), "serial": int(info["serial"]), "version": str(info["version"])}
        except (OSError, ValueError, KeyError, TypeError):
            log.exception("shell.json missing or broken; updates are off")
            return {"shell": "", "serial": 0, "version": "unknown"}
    return {"shell": fingerprint(root), "serial": 0, "version": "source"}


def enabled_here(frozen: bool) -> bool:
    """Bundles only; SIMPLIXITY_DESKTOP_UPDATES=1 tries it from source."""
    return frozen or os.environ.get("SIMPLIXITY_DESKTOP_UPDATES") == "1"


# --- downloads -------------------------------------------------------------


def _client():
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(30, read=60),
        follow_redirects=True,
        headers={"User-Agent": "simplixity-desktop-updater"},
    )


def _github_sha256(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise UpdateError(f"GitHub reports no checksum for {asset.get('name')}; not installing.")
    return digest[7:]


def _download(client, asset: dict, out, limit: int) -> None:
    """Stream a release asset into `out`; it must match GitHub's SHA-256."""
    expected = _github_sha256(asset)
    h, n = hashlib.sha256(), 0
    with client.stream("GET", asset["browser_download_url"]) as r:
        if r.status_code != 200:
            raise UpdateError(f"Download failed: HTTP {r.status_code}.")
        for chunk in r.iter_bytes():
            n += len(chunk)
            if n > limit:
                raise UpdateError("The download is larger than expected.")
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != expected:
        raise UpdateError(f"{asset['name']} does not match GitHub's checksum; not installing.")


def _latest(client) -> dict | None:
    """The newest release's payload for this platform: manifest, zip asset, page."""
    r = client.get(LATEST_RELEASE, headers={"Accept": "application/vnd.github+json"})
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise UpdateError(f"GitHub did not answer (HTTP {r.status_code}).")
    release = r.json()
    assets = {a.get("name"): a for a in release.get("assets", []) if isinstance(a, dict)}
    manifest_asset, zip_asset = assets.get(payload_name() + ".json"), assets.get(payload_name() + ".zip")
    if not manifest_asset or not zip_asset:
        return None  # a release without an update for this platform
    buf = io.BytesIO()
    _download(client, manifest_asset, buf, MAX_MANIFEST)
    try:
        m = json.loads(buf.getvalue())
        manifest = {"version": str(m["version"]), "serial": int(m["serial"]), "shell": str(m["shell"])}
    except (ValueError, KeyError, TypeError):
        raise UpdateError("The release's update description is unreadable.") from None
    return {"manifest": manifest, "zip": zip_asset, "page": release.get("html_url")}


def _extract(zip_path: Path, dest: Path) -> None:
    dest.mkdir()
    seen: set[str] = set()
    with zipfile.ZipFile(zip_path) as zf:
        # zipfile stops each entry at its stated size, so the sum bounds the disk used.
        if sum(i.file_size for i in zf.infolist()) > MAX_UNPACKED:
            raise UpdateError("The update unpacks to more than it may.")
        for info in zf.infolist():
            name = info.filename
            # Checked as written: PurePosixPath would quietly drop a "." part.
            raw = name.removesuffix("/").split("/") if info.is_dir() else name.split("/")
            parts = PurePosixPath(name).parts
            key = "/".join(parts).lower()  # case-insensitive file systems
            bad = (
                not parts
                or name.startswith("/")
                or "\\" in name
                or ":" in name
                or any(p in ("", ".", "..") for p in raw)
                or parts[0] not in PAYLOAD_TOP
                or stat.S_ISLNK(info.external_attr >> 16)
                or key in seen
            )
            if bad:
                raise UpdateError(f"The update contains an entry it must not: {name!r}.")
            seen.add(key)
            target = dest.joinpath(*parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "xb") as out:
                shutil.copyfileobj(src, out)
    missing = PAYLOAD_REQUIRED - {p.name for p in dest.iterdir()}
    if missing:
        raise UpdateError(f"The update lacks {', '.join(sorted(missing))}.")


# --- the store -------------------------------------------------------------


class Store:
    def __init__(self, data_dir: Path, shell: dict, enabled: bool) -> None:
        self.root = data_dir / "payload"
        self.shell = shell
        self.enabled = enabled and bool(shell["shell"])
        self.running: dict | None = None  # manifest of the payload in use; None: built-in
        self.found: dict | None = None    # what the last check found
        self.error: str | None = None
        self.busy = threading.Lock()

    # state.json: {"active", "previous": serial or None, "pending", "attempted": bool}
    def _state(self) -> dict:
        st = {"active": None, "previous": None, "pending": False, "attempted": False}
        try:
            st.update(json.loads((self.root / "state.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        return st

    def _save(self, st: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "state.json.tmp"
        tmp.write_text(json.dumps(st), encoding="utf-8")
        os.replace(tmp, self.root / "state.json")

    def _installed(self, serial: int | None) -> dict | None:
        if serial is None:
            return None
        try:
            m = json.loads((self.root / str(serial) / "payload.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return m if m.get("serial") == serial and m.get("shell") == self.shell["shell"] else None

    def _newest_here(self) -> int:
        st = self._state()
        staged = self._installed(st["active"]) if st["pending"] else None
        return max([self.shell["serial"], *(m["serial"] for m in (self.running, staged) if m)])

    # -- start ------------------------------------------------------------

    def boot(self) -> Path | None:
        """The payload folder to run from, or None for the built-in code."""
        if not self.enabled:
            return None
        st = self._state()
        if st["pending"] and st["attempted"]:
            log.warning("update %s did not start last time; going back", st["active"])
            st.update(active=st["previous"], previous=None, pending=False, attempted=False)
            self._save(st)
        m = self._installed(st["active"])
        if m is None or m["serial"] <= self.shell["serial"]:
            # Gone, for another shell, or older than the app's own code.
            if st["active"] is not None:
                st.update(active=None, previous=None, pending=False, attempted=False)
                self._save(st)
            return None
        if st["pending"]:
            st["attempted"] = True
            self._save(st)
        self.running = m
        log.info("running update %s", m["version"])
        return self.root / str(m["serial"])

    def confirm(self) -> None:
        """The server came up: keep this payload, drop the ones nobody returns to."""
        if not self.enabled:
            return
        st = self._state()
        if st["pending"] and self.running and self.running["serial"] == st["active"]:
            st.update(pending=False, attempted=False)
            self._save(st)
        if st["pending"] or not self.root.is_dir():
            return
        keep = {str(st["active"]), str(st["previous"])}
        for d in self.root.iterdir():
            if d.is_dir() and d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)

    def check_in_background(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.check()
                except Exception:  # noqa: BLE001 - offline, rate limit: try again later
                    log.info("update check failed: %s", self.error)
                time.sleep(CHECK_EVERY)

        if self.enabled:
            threading.Thread(target=loop, name="update-check", daemon=True).start()

    # -- what the window asks ---------------------------------------------

    def status(self) -> dict:
        st = self._state()
        staged = self._installed(st["active"]) if st["pending"] else None
        if staged and self.running and staged["serial"] == self.running["serial"]:
            staged = None
        offer = None
        if self.found and self.found["manifest"]["serial"] > self._newest_here():
            m = self.found["manifest"]
            offer = {"version": m["version"], "installer_only": m["shell"] != self.shell["shell"],
                     "page": self.found["page"]}
        return {
            "enabled": self.enabled,
            "version": self.running["version"] if self.running else self.shell["version"],
            "available": offer,
            "staged": staged["version"] if staged else None,
            "busy": self.busy.locked(),
            "error": self.error,
        }

    def check(self) -> dict:
        if not self.enabled:
            raise UpdateError("Updates only work in the installed app.")
        try:
            with _client() as client:
                self.found = _latest(client)
            self.error = None
        except Exception as e:
            self.error = str(e) if isinstance(e, UpdateError) else f"Could not reach GitHub ({type(e).__name__})."
            raise
        return self.status()

    def install(self) -> dict:
        if not self.busy.acquire(blocking=False):
            raise UpdateError("An update is already being installed.")
        tmp = None
        try:
            with _client() as client:
                found = _latest(client)  # again: the release may have changed since the check
                if not found or found["manifest"]["serial"] <= self._newest_here():
                    raise UpdateError("There is no newer update.")
                m = found["manifest"]
                if m["shell"] != self.shell["shell"]:
                    raise UpdateError("This update needs a new installer.")
                self.root.mkdir(parents=True, exist_ok=True)
                tmp = Path(tempfile.mkdtemp(dir=self.root, prefix=".incoming-"))
                zip_path = tmp / "payload.zip"
                with open(zip_path, "wb") as out:
                    _download(client, found["zip"], out, MAX_ZIP)
            tree = tmp / "tree"
            _extract(zip_path, tree)
            # Compile now, so the next start is not the slow one. One worker:
            # a process pool would make a bundle start itself again.
            try:
                import compileall

                compileall.compile_dir(tree, ddir=str(self.root / str(m["serial"])), quiet=1, workers=1)
            except Exception:  # noqa: BLE001
                log.exception("could not precompile the update")
            (tree / "payload.json").write_text(json.dumps(m), encoding="utf-8")
            dest = self.root / str(m["serial"])
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(tree, dest)
            st = self._state()
            st.update(active=m["serial"], previous=self.running["serial"] if self.running else None,
                      pending=True, attempted=False)
            self._save(st)
            self.found = found
            log.info("update %s installed; active on next start", m["version"])
            return self.status()
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            self.busy.release()
