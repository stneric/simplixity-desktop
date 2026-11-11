"""What the desktop app does differently from the server: one user, keys in the app.

A server has many people, so Simplixity has profiles and a picker, and its API
keys come from the operator's environment. The app has one person, so it

- signs every request into one profile itself, silently: no picker, no
  switching, never shown. The profile is the one already there (so chats
  made before carry over; one without a password first), or "me", created
  on a fresh install. Remembered in desktop.json. A password on it is not
  asked for: whoever can open the app owns this computer's data anyway.
- lets that person enter the API keys: /desktop/api/keys reads and writes them
  (into the data directory's .env, the file Simplixity reads at start) and
  applies them to the running app at once. skin/skin.js adds the "API keys"
  tab to Settings.
- reports and installs app updates: /desktop/api/update (status, also what
  the background check found, and the install's progress), update/check,
  update/install (starts it in the background), update/restart. The work is
  updater.py's (see UpdateProgress); the toast and the "Updates" tab are
  skin/skin.js.
- offers Simplixity's own local models when Ollama is there, found on
  Hugging Face, and keeps the ones Ollama has at their newest version:
  /desktop/api/local-models lists them (and whether Ollama runs and has
  them), local-models/<key>/install downloads one into Ollama and sets it up,
  local-models/<key>/cancel stops that. skin/skin.js shows the offer under
  the matching model picker in Settings. See LocalModels.
- answers only its own window. Every request is signed in, so the server
  must tell the window from other web pages that can reach 127.0.0.1: a
  Host header that is not this server's (DNS rebinding: evil.example
  resolving to 127.0.0.1 counts as same-origin to the browser) and an
  Origin from another site (a form posted from any page) are refused.

Upstream is used through its own functions only (store.profiles, the
settings object, the session cookie name). If any of them changes, the
wrapper stops doing its part and logs why: upstream's profile picker comes
back, the keys tab says it cannot save. Nothing else breaks.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("simplixity.desktop")

PREFIX = "/desktop/api/"
# A header a cross-site form or fetch cannot send without a CORS preflight,
# which this server never answers: only the app's own page can write keys.
WRITE_HEADER = b"x-simplixity-desktop"

# (env var, label, what it is for, where to get one). Offered only if the
# running Simplixity has a setting of that name.
KEYS = [
    ("OPENROUTER_API_KEY", "OpenRouter", "Chat models and embeddings. Needed unless you only use local models (Ollama, LM Studio).", "https://openrouter.ai/settings/keys"),
    ("GROQ_API_KEY", "Groq", "Optional second provider; its models join the picker.", "https://console.groq.com/keys"),
    ("JINA_API_KEY", "Jina Reader", "Optional. Higher rate limits when reading pages.", "https://jina.ai/reader"),
    ("IEEE_API_KEY", "IEEE Xplore", "Literature page, optional.", "https://developer.ieee.org"),
    ("SEMANTIC_SCHOLAR_API_KEY", "Semantic Scholar", "Literature page, optional. Free on request.", "https://www.semanticscholar.org/product/api"),
    ("OPENALEX_API_KEY", "OpenAlex", "Literature page, optional.", "https://openalex.org"),
]

# ── Simplixity's own local models ─────────────────────────────────────────
#
# Nothing about them is written here: the app finds them on Hugging Face. Every
# repo of CATALOG_AUTHOR that carries a `simplixity.json` is one model:
#
#   {"setting": "chat",                  which Settings picker offers it
#    "label": "Simplixity Writer 4B", "about": "...",
#    "tag": "simplixity-writer:4b",      the Ollama name, the same for every version
#    "quant": "Q4_K_M",                  which GGUF of the repo to use
#    "ollama": {"renderer": ..., "parser": ..., "parameters": {...}}}
#
# The GGUF used is the highest version of that quant: `...-v<N>-<quant>.gguf`.
# Publishing v2 is uploading `...-v2-Q4_K_M.gguf`; every app that has the model
# fetches it within CHECK_EVERY and puts it under the same tag, so the setting
# that names the tag keeps working. A file comes as that exact file, streamed
# from Hugging Face into Ollama, which checks it against the sha256 Hugging
# Face reports. (Ollama's own `hf.co/<repo>:<quant>` takes the first file of a
# quant, which in a repo with v1 and v4 is v1.)
#
# Models from the Ollama library (LIBRARY) are pulled by name; a pull of a
# model Ollama has is how it gets that model's newest version.

HF = "https://huggingface.co"
CATALOG_AUTHOR = "stneric"
MANIFEST = "simplixity.json"
USER_AGENT = "simplixity-desktop"
CHECK_EVERY = 6 * 3600   # seconds: catalogue and model updates, while the app is open
FIRST_CHECK = 60         # after the start, so it does not compete with it
# The app's settings, by the Settings picker that sets each.
SLOTS = {"chat": "settings-chat-model", "router": "router-model", "judge": "judge-model",
         "title": "title-model", "embedding": "embedding-model"}
LIBRARY = [
    {"key": "embeddinggemma", "setting": "embedding", "label": "EmbeddingGemma", "size": 621_875_917,
     "about": "Ranks the passages a search finds, on this computer. Google's model, from the Ollama library.",
     "tag": "embeddinggemma:latest", "pull": "embeddinggemma", "page": "https://ollama.com/library/embeddinggemma"},
]


def _version_of(path: str, quant: str) -> int | None:
    m = re.search(rf"-v(\d+)-{re.escape(quant)}\.gguf$", path, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _model_from(repo: str, manifest: dict, tree: list[dict]) -> dict | None:
    """One catalogue entry from a repo's simplixity.json and file list, or None."""
    try:
        quant = str(manifest.get("quant") or "Q4_K_M")
        files = [(v, f) for f in tree if isinstance(f, dict)
                 and (v := _version_of(str(f.get("path", "")), quant)) is not None and (f.get("lfs") or {}).get("oid")]
        if not files or manifest.get("setting") not in SLOTS or ":" not in str(manifest.get("tag", "")):
            return None
        version, f = max(files, key=lambda vf: vf[0])
        return {
            "key": repo.split("/")[-1].removesuffix("-GGUF").lower(), "setting": manifest["setting"],
            "label": str(manifest.get("label") or repo.split("/")[-1]), "about": str(manifest.get("about") or ""),
            "tag": str(manifest["tag"]), "repo": repo, "file": f["path"], "version": f"v{version}",
            "digest": f"sha256:{f['lfs']['oid']}", "size": int(f["lfs"].get("size") or f.get("size") or 0),
            "create": {k: v for k, v in (manifest.get("ollama") or {}).items() if k in ("renderer", "parser", "parameters", "template", "system")},
            "page": f"{HF}/{repo}",
        }
    except (TypeError, ValueError, KeyError, AttributeError):
        log.warning("ignoring %s: its %s is not understood", repo, MANIFEST)
        return None


async def discover(client) -> list[dict]:
    """Simplixity's models on Hugging Face, newest version of each."""
    import asyncio

    resp = await client.get(f"{HF}/api/models", params={"author": CATALOG_AUTHOR, "search": "simplixity", "limit": 200})
    resp.raise_for_status()
    repos = [r["id"] for r in resp.json() if isinstance(r, dict) and r.get("id")]

    async def one(repo: str) -> dict | None:
        try:
            tree = (await client.get(f"{HF}/api/models/{repo}/tree/main")).raise_for_status().json()
            if not any(isinstance(f, dict) and f.get("path") == MANIFEST for f in tree):
                return None
            manifest = (await client.get(f"{HF}/{repo}/resolve/main/{MANIFEST}", follow_redirects=True)).raise_for_status().json()
            return _model_from(repo, manifest, tree)
        except Exception:  # noqa: BLE001 - one broken repo must not hide the others
            log.warning("could not read %s from Hugging Face", repo, exc_info=True)
            return None

    found = [m for m in await asyncio.gather(*(one(r) for r in repos)) if m]
    return sorted(found, key=lambda m: list(SLOTS).index(m["setting"]))


class LocalModels:
    """Downloads Simplixity's models into Ollama and keeps them current.

    One job per model, in the server's event loop; a job is kept after it
    ends, so the window can say how it went. keep_current() runs for as long
    as the app is open: it refreshes the catalogue and updates every model
    Ollama has to its newest version.
    """

    def __init__(self, base_url, cache_file: Path | None = None):
        self.base_url = base_url   # a function: the Ollama address is a setting
        self.cache_file = cache_file
        self.catalog: list[dict] = []
        self.checked = 0.0
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, Any] = {}
        self.loop_task = None
        try:  # what the last start found, so the offers are there offline too
            self.catalog = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file else []
        except (OSError, ValueError):
            pass

    def _client(self, **kw):
        import httpx

        return httpx.AsyncClient(headers={"user-agent": USER_AGENT}, **kw)

    async def refresh(self) -> None:
        import time

        async with self._client(timeout=15) as client:
            self.catalog = await discover(client)
        self.checked = time.monotonic()
        if self.cache_file:
            try:
                self.cache_file.write_text(json.dumps(self.catalog), encoding="utf-8")
            except OSError:
                pass

    def models(self) -> list[dict]:
        return [*self.catalog, *LIBRARY]

    async def _ollama(self, client) -> tuple[set[str] | None, dict[str, str]]:
        """The model names Ollama has (None: not reachable), and the file each of ours is."""
        base = self.base_url().rstrip("/")
        try:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            # Ollama's names do not care about case: q4_K_M is q4_k_m.
            tags = {(t.get("name") or t.get("model") or "").lower() for t in resp.json().get("models", [])}
        except Exception:  # noqa: BLE001 - not running, or not Ollama
            return None, {}
        files: dict[str, str] = {}
        for m in self.catalog:
            if m["tag"].lower() in tags:
                try:
                    show = (await client.post(f"{base}/api/show", json={"model": m["tag"]})).json()
                    found = re.search(r"sha256[-:]([0-9a-f]{64})", show.get("modelfile", ""))
                    if found:
                        files[m["key"]] = f"sha256:{found.group(1)}"
                except Exception:  # noqa: BLE001
                    pass
        return tags, files

    async def status(self) -> dict:
        import shutil
        import time

        if not self.catalog or time.monotonic() - self.checked > CHECK_EVERY:
            try:
                await self.refresh()
            except Exception:  # noqa: BLE001 - offline: what the last start found
                log.info("could not read the model catalogue from Hugging Face")
                self.checked = time.monotonic() - CHECK_EVERY + 300  # again in 5 minutes
        async with self._client(timeout=3) as client:
            tags, files = await self._ollama(client)
        base = self.base_url().rstrip("/")
        local = any(h in base for h in ("://localhost", "://127.0.0.1", "://[::1]"))
        out = []
        for m in self.models():
            installed = tags is not None and m["tag"].lower() in tags
            out.append({
                "key": m["key"], "slot": SLOTS[m["setting"]], "label": m["label"], "about": m["about"],
                "size": m["size"], "tag": m["tag"], "page": m["page"], "version": m.get("version"),
                "installed": installed,
                # Installed, but not the newest file: keep_current() replaces it.
                "outdated": installed and "digest" in m and m["key"] in files and files[m["key"]] != m["digest"],
                "job": self.jobs.get(m["key"]),
            })
        return {"ollama": {"running": tags is not None,
                           "installed": tags is not None or (local and bool(shutil.which("ollama"))), "url": base},
                "models": out}

    def install(self, key: str, update: bool = False, quiet: bool = False) -> None:
        import asyncio

        model = next((m for m in self.models() if m["key"] == key), None)
        if model is None:
            raise KeyError(key)
        if key in self.tasks and not self.tasks[key].done():
            return
        self.jobs[key] = {"state": "starting", "done": 0, "total": model["size"], "error": None,
                          "update": update, "quiet": quiet, "version": model.get("version")}
        self.tasks[key] = asyncio.get_running_loop().create_task(self._run(model, self.jobs[key]))

    def cancel(self, key: str) -> None:
        task = self.tasks.get(key)
        if task and not task.done():
            task.cancel()

    def start(self) -> None:
        """keep_current(), once, in the running event loop."""
        import asyncio

        if self.loop_task is None:
            self.loop_task = asyncio.get_running_loop().create_task(self.keep_current())

    async def keep_current(self) -> None:
        import asyncio

        await asyncio.sleep(FIRST_CHECK)
        while True:
            try:
                await self.refresh()
                async with self._client(timeout=10) as client:
                    tags, files = await self._ollama(client)
                for m in self.catalog if tags is not None else []:
                    if m["key"] in files and files[m["key"]] != m["digest"]:
                        log.info("updating %s to %s", m["tag"], m["version"])
                        self.install(m["key"], update=True)
                for m in LIBRARY if tags is not None else []:
                    if m["tag"].lower() in tags:
                        # A pull: quick, and nothing to show, when nothing changed.
                        self.install(m["key"], update=True, quiet=True)
            except Exception:  # noqa: BLE001 - offline: next time
                log.info("model update check failed", exc_info=True)
            await asyncio.sleep(CHECK_EVERY)

    async def _run(self, model: dict, job: dict) -> None:
        import asyncio

        import httpx

        base = self.base_url().rstrip("/")
        try:
            # No read timeout: a pull or a create can be quiet for a long while.
            async with self._client(timeout=httpx.Timeout(30, read=None)) as client:
                if "pull" in model:
                    await self._pull(client, base, model, job)
                else:
                    await self._from_hf(client, base, model, job)
            job.update(state="done", done=job["total"])
            log.info("local model %s is %s", model["tag"], model.get("version") or "current")
        except asyncio.CancelledError:
            job.update(state="cancelled", error=None)
            log.info("local model %s: download cancelled", model["key"])
        except Exception as e:  # noqa: BLE001 - shown in the window
            job.update(state="error", error=str(e) or type(e).__name__)
            log.exception("local model %s: install failed", model["key"])

    @staticmethod
    async def _pull(client, base: str, model: dict, job: dict) -> None:
        job["state"] = "downloading"
        layers: dict[str, tuple[int, int]] = {}
        async with client.stream("POST", f"{base}/api/pull", json={"model": model["pull"]}) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                if msg.get("digest") and msg.get("total"):
                    layers[msg["digest"]] = (msg.get("completed") or 0, msg["total"])
                    job["done"] = sum(c for c, _ in layers.values())
                    job["total"] = sum(t for _, t in layers.values())
                elif msg.get("status", "").startswith(("verifying", "writing")):
                    job["state"] = "creating"

    @staticmethod
    async def _from_hf(client, base: str, model: dict, job: dict) -> None:
        digest = model["digest"]
        # A file Ollama has already (another tag, a create that failed) is not sent again.
        if (await client.head(f"{base}/api/blobs/{digest}")).status_code != 200:
            job["state"] = "downloading"
            url = f"{HF}/{model['repo']}/resolve/main/{model['file']}"
            async with client.stream("GET", url, follow_redirects=True) as src:
                src.raise_for_status()

                async def chunks():
                    async for chunk in src.aiter_bytes(1 << 20):
                        job["done"] += len(chunk)
                        yield chunk

                # Straight into Ollama, which checks it against the digest.
                up = await client.post(f"{base}/api/blobs/{digest}", content=chunks(),
                                       headers={"content-length": str(model["size"])})
                if up.status_code not in (200, 201):
                    raise RuntimeError(f"Ollama refused the file: {up.text.strip() or up.status_code}")
        job.update(state="creating", done=job["total"])
        resp = await client.post(f"{base}/api/create", json={
            "model": model["tag"], "files": {model["file"]: digest}, "stream": False, **model["create"]})
        if resp.status_code != 200 or resp.json().get("error"):
            raise RuntimeError(f"Ollama could not set up {model['tag']}: {resp.text.strip()}")


# ── App updates: progress and restart ─────────────────────────────────────
#
# updater.py is part of the installed shell, and a change to it would need a
# new installer everywhere. So what the window shows while an update installs
# is added from here: the install runs in a thread of its own (the request
# returns at once), and updater._download is wrapped to count the bytes of the
# payload ZIP as they arrive. If updater.py ever drops _download, the install
# still works, only without a byte count.


class UpdateProgress:
    def __init__(self, updates):
        self.updates = updates
        self.state: dict | None = None   # {"phase", "done", "total", "error", "version"}
        self.thread = None

    def hook(self, updater) -> None:
        original = getattr(updater, "_download", None)
        if original is None or getattr(original, "_counted", False):
            return
        progress = self

        def counted(client, asset, out, limit):
            if not str(asset.get("name", "")).endswith(".zip") or progress.state is None:
                return original(client, asset, out, limit)
            progress.state.update(phase="downloading", done=0, total=int(asset.get("size") or 0))

            class Counting:
                def write(self, chunk):
                    progress.state["done"] += len(chunk)
                    return out.write(chunk)

            original(client, asset, Counting(), limit)
            progress.state.update(phase="unpacking", done=progress.state["total"])

        counted._counted = True
        updater._download = counted

    def start(self) -> None:
        import threading

        if self.thread and self.thread.is_alive():
            return
        found = self.updates.status().get("available") or {}
        self.state = {"phase": "starting", "done": 0, "total": 0, "error": None, "version": found.get("version")}

        def run():
            try:
                self.updates.install()
                self.state = None   # status() now reports it as staged
            except Exception as e:  # noqa: BLE001 - shown in the window
                log.exception("update install failed")
                self.state = {**(self.state or {}), "phase": "error", "error": str(e) or type(e).__name__}

        self.thread = threading.Thread(target=run, name="update-install", daemon=True)
        self.thread.start()

    def status(self) -> dict:
        st = self.updates.status()
        installing = self.thread is not None and self.thread.is_alive()
        # updater's own "busy" is read inside its lock, so its answer to the
        # install itself always said busy; the thread is what counts.
        st["busy"] = installing
        st["progress"] = self.state if (installing or (self.state or {}).get("phase") == "error") else None
        return st


def relaunch_command(pid: int) -> list[str]:
    """A command that waits for this process to end, then starts the app again."""
    import shlex
    import sys

    if not getattr(sys, "frozen", False):
        main = sys.modules.get("__main__")
        target = [sys.executable, os.path.abspath(getattr(main, "__file__", "simplixity_desktop.py"))]
    elif sys.platform == "darwin":
        app = Path(sys.executable).resolve().parents[2]   # Simplixity.app/Contents/MacOS/Simplixity
        target = ["open", "-n", str(app)] if app.suffix == ".app" else [sys.executable]
    elif sys.platform == "win32":
        exe = sys.executable.replace("'", "''")
        return ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                f"Wait-Process -Id {pid} -ErrorAction SilentlyContinue; Start-Process '{exe}'"]
    else:
        # In an AppImage, sys.executable lives in a mount that goes away with this process.
        target = [os.environ.get("APPIMAGE") or sys.executable]
    return ["/bin/sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; exec {shlex.join(target)}"]


def restart_app() -> None:
    """Start the app again once this one has ended, then end this one."""
    import subprocess
    import sys
    import threading

    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(relaunch_command(os.getpid()), **kw)
    log.info("restarting for the update")

    def close():
        # Closing the window ends the launcher's main() the normal way (the
        # server stops, the window size is saved); a hard exit if it does not.
        windows = []
        try:
            import webview

            windows = list(webview.windows)
            for w in windows:
                w.destroy()
        except Exception:  # noqa: BLE001
            log.exception("could not close the window")
        if not windows:
            os._exit(0)   # headless: no window to close
        threading.Timer(8, lambda: os._exit(0)).start()

    threading.Timer(0.3, close).start()   # the answer to the request goes out first


def _own_origins(scope) -> tuple[set[bytes], set[bytes]]:
    """The Host values and Origins this server answers to: loopback, its own port."""
    port = (scope.get("server") or (None, None))[1]
    hosts = {f"{h}:{port}".encode() for h in ("127.0.0.1", "localhost", "[::1]")}
    return hosts, {b"http://" + h for h in hosts}


def request_allowed(scope) -> bool:
    """From the app's own page (or a browser on that port, headless), nothing else."""
    headers = dict(scope.get("headers") or [])
    hosts, origins = _own_origins(scope)
    if headers.get(b"host", b"").lower() not in hosts:
        return False
    origin = headers.get(b"origin")
    if origin is not None and origin.lower() not in origins:
        return False
    # Without an Origin (a cross-site GET navigation, an image), the browser still says where it comes from.
    return headers.get(b"sec-fetch-site", b"same-origin") in (b"same-origin", b"none")


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _offered(settings) -> list[tuple[str, str, str, str]]:
    fields = type(settings).model_fields
    return [k for k in KEYS if k[0].lower() in fields]


def _hint(value: str | None) -> str | None:
    return f"…{value[-4:]}" if value and len(value) > 8 else ("set" if value else None)


def write_env(path: Path, changes: dict[str, str]) -> None:
    """Set (or, for "", remove) variables in a .env, keeping every other line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], set()
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and m.group(1) in changes:
            name = m.group(1)
            if name not in done and changes[name]:
                out.append(f"{name}={changes[name]}")
            done.add(name)
            continue
        out.append(line)
    out += [f"{k}={v}" for k, v in changes.items() if v and k not in done]
    # Created 0600 and moved into place: the keys are never readable by other
    # users, and a crash mid-write leaves the old file, not half of one.
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def wrap(app, upstream, data_dir: Path, updates=None):
    """`app` is what gets called (upstream behind the skin), `upstream` the
    FastAPI object, for its state (db, redis) once its lifespan has run,
    `updates` the updater.Store behind Settings -> Updates."""
    import asyncio

    from app.config import settings

    import updater

    env_file = data_dir / ".env"
    state_file = data_dir / "desktop.json"
    session: dict[str, Any] = {"cookie": None, "tried": False}
    local_models = LocalModels(lambda: settings.ollama_base_url, data_dir / "local-models.json")
    progress = UpdateProgress(updates) if updates is not None else None
    if progress:
        progress.hook(updater)

    async def clear_model_cache() -> None:
        # The model lists are cached; what is in them just changed.
        try:
            redis = upstream.state.redis
            async for k in redis.scan_iter(match="models:*"):
                await redis.delete(k)
        except Exception:  # noqa: BLE001
            log.exception("could not clear the model cache")

    async def session_cookie() -> bytes | None:
        if session["tried"]:
            return session["cookie"]
        session["tried"] = True
        try:
            from app.api.deps import SESSION_COOKIE
            from app.store import profiles

            db = upstream.state.db
            try:
                name = json.loads(state_file.read_text(encoding="utf-8"))["profile"]
            except (OSError, ValueError, KeyError):
                name = None
            profile = await profiles.get(db, name) if name else None
            if profile is None:
                # The profile already there (chats made before carry over),
                # one without a password first; a fresh install gets "me".
                existing = await profiles.list_profiles(db)
                existing.sort(key=lambda p: p["protected"])
                name = existing[0]["name"] if existing else "me"
                if not existing:
                    await profiles.create(db, name)
                profile = await profiles.get(db, name)
                state_file.write_text(json.dumps({"profile": name}), encoding="utf-8")
            token = await profiles.session_token(db, profile)
            session["cookie"] = f"{SESSION_COOKIE}={token}".encode()
            log.info("signed in as profile %s", name)
        except Exception:  # noqa: BLE001 - upstream changed: fall back to its picker
            log.exception("could not sign in automatically; the profile picker stays")
        return session["cookie"]

    async def respond(send, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    def describe() -> dict:
        return {"keys": [
            {"env": env, "label": label, "help": help_, "url": url, "set": _hint(getattr(settings, env.lower()))}
            for env, label, help_, url in _offered(settings)
        ]}

    async def read_body(receive) -> bytes:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                return body

    def may_write(scope, method: str) -> bool:
        return scope["method"] == method and dict(scope["headers"]).get(WRITE_HEADER) == b"1"

    async def update(scope, receive, send, action: str) -> None:
        """GET update: status, with the install's progress. POST update/check;
        update/install starts the install and returns at once; update/restart."""
        if updates is None or progress is None:
            return await respond(send, 404, {"detail": "not found"})
        if action == "":
            if scope["method"] != "GET":
                return await respond(send, 403, {"detail": "forbidden"})
            return await respond(send, 200, progress.status())
        if action not in ("check", "install", "restart") or not may_write(scope, "POST"):
            return await respond(send, 403, {"detail": "forbidden"})
        await read_body(receive)
        if action == "install":
            progress.start()
            return await respond(send, 200, progress.status())
        if action == "restart":
            if not updates.status().get("staged"):
                return await respond(send, 409, {"detail": "No update is waiting for a restart."})
            restart_app()
            return await respond(send, 200, {"restarting": True})
        try:
            await asyncio.to_thread(updates.check)
            result = progress.status()
        except updater.UpdateError as e:
            log.warning("update %s refused: %s", action, e)
            return await respond(send, 409, {"detail": str(e)})
        except Exception as e:  # noqa: BLE001 - network and disk errors, shown in the window
            log.exception("update %s failed", action)
            return await respond(send, 502, {"detail": f"{type(e).__name__}: {e}"})
        return await respond(send, 200, result)

    async def keys(scope, receive, send) -> None:
        if scope["method"] == "GET":
            return await respond(send, 200, describe())
        if not may_write(scope, "PUT"):
            return await respond(send, 403, {"detail": "forbidden"})
        body = await read_body(receive)
        try:
            changes = json.loads(body)
            offered = {k[0] for k in _offered(settings)}
            changes = {k: "" if v is None else str(v).strip() for k, v in changes.items() if k in offered}
            if any(re.search(r"\s", v) for v in changes.values()):
                return await respond(send, 400, {"detail": "A key has no spaces or line breaks."})
        except (ValueError, AttributeError):
            return await respond(send, 400, {"detail": "bad request"})
        if changes:
            write_env(env_file, changes)
            for env, value in changes.items():
                setattr(settings, env.lower(), value or None)
                if value:
                    os.environ[env] = value
                else:
                    os.environ.pop(env, None)
            # The model lists depend on the keys.
            await clear_model_cache()
            log.info("API keys changed: %s", ", ".join(sorted(changes)))
        return await respond(send, 200, describe())

    async def models(scope, receive, send, action: str) -> None:
        """GET local-models: what is offered and how far. POST <key>/install, <key>/cancel."""
        if action == "":
            if scope["method"] != "GET":
                return await respond(send, 403, {"detail": "forbidden"})
            st = await local_models.status()
            # A finished job's model is new to the pickers; once is enough.
            for m in st["models"]:
                job = m["job"]
                if job and job["state"] == "done" and not job.get("announced"):
                    job["announced"] = True
                    await clear_model_cache()
            return await respond(send, 200, st)
        key, _, verb = action.partition("/")
        if verb not in ("install", "cancel") or not may_write(scope, "POST"):
            return await respond(send, 403, {"detail": "forbidden"})
        await read_body(receive)
        try:
            local_models.install(key) if verb == "install" else local_models.cancel(key)
        except KeyError:
            return await respond(send, 404, {"detail": "not found"})
        return await respond(send, 200, await local_models.status())

    async def asgi(scope, receive, send):
        if scope["type"] == "websocket" and not request_allowed(scope):
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return await app(scope, receive, send)
        if not request_allowed(scope):
            log.warning("refused %s %s from another site", scope["method"], scope["path"])
            return await respond(send, 403, {"detail": "forbidden"})
        local_models.start()   # keeps Simplixity's models current, from the first request on
        path = scope["path"]
        if path == PREFIX + "keys":
            return await keys(scope, receive, send)
        if path == PREFIX + "update" or path.startswith(PREFIX + "update/"):
            return await update(scope, receive, send, path[len(PREFIX + "update"):].lstrip("/"))
        if path == PREFIX + "local-models" or path.startswith(PREFIX + "local-models/"):
            return await models(scope, receive, send, path[len(PREFIX + "local-models"):].lstrip("/"))
        cookie = await session_cookie()
        if cookie:
            headers = [(k, v) for k, v in scope["headers"] if k != b"cookie"]
            scope = {**scope, "headers": headers + [(b"cookie", cookie)]}
        return await app(scope, receive, send)

    return asgi
