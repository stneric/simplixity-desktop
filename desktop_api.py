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
  the background check found), update/check, update/install. The work is
  updater.py's; the notice and the "Updates" tab are skin/skin.js.
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
        """GET update: status. POST update/check, update/install."""
        if updates is None:
            return await respond(send, 404, {"detail": "not found"})
        if action == "":
            if scope["method"] != "GET":
                return await respond(send, 403, {"detail": "forbidden"})
            return await respond(send, 200, updates.status())
        if action not in ("check", "install") or not may_write(scope, "POST"):
            return await respond(send, 403, {"detail": "forbidden"})
        await read_body(receive)
        try:
            result = await asyncio.to_thread(updates.check if action == "check" else updates.install)
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
            # The model lists are cached for an hour; they depend on the keys.
            try:
                redis = upstream.state.redis
                async for k in redis.scan_iter(match="models:*"):
                    await redis.delete(k)
            except Exception:  # noqa: BLE001
                log.exception("could not clear the model cache")
            log.info("API keys changed: %s", ", ".join(sorted(changes)))
        return await respond(send, 200, describe())

    async def asgi(scope, receive, send):
        if scope["type"] == "websocket" and not request_allowed(scope):
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return await app(scope, receive, send)
        if not request_allowed(scope):
            log.warning("refused %s %s from another site", scope["method"], scope["path"])
            return await respond(send, 403, {"detail": "forbidden"})
        path = scope["path"]
        if path == PREFIX + "keys":
            return await keys(scope, receive, send)
        if path == PREFIX + "update" or path.startswith(PREFIX + "update/"):
            return await update(scope, receive, send, path[len(PREFIX + "update"):].lstrip("/"))
        cookie = await session_cookie()
        if cookie:
            headers = [(k, v) for k, v in scope["headers"] if k != b"cookie"]
            scope = {**scope, "headers": headers + [(b"cookie", cookie)]}
        return await app(scope, receive, send)

    return asgi
