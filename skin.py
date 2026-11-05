"""The desktop skin: Simplixity's own pages, plus a stylesheet and a script.

Simplixity is served unmodified. This ASGI wrapper sits in front of it and

- adds `<link>`/`<script>` tags for skin/ to every HTML page, right before
  `</head>`, so the skin loads after styles.css and wins ties: skin.css and
  skin.js everywhere, <platform>.css (macos.css, ...) where one exists.
  External files only, because upstream's CSP allows no inline script;
- serves skin/ under /desktop/;
- answers /sw.js with a worker that removes itself: an app window has no use
  for an offline shell, and a cached one would outlive an app update.

Everything else passes through untouched, streams (SSE) included. If upstream
ever drops `</head>` the page is served as-is, without the skin: it looks like
the browser version, it does not break.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

PREFIX = "/desktop/"

KILL_WORKER = b"""// Simplixity Desktop: no service worker in the app window. Removes any left behind.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil((async () => {
  for (const key of await caches.keys()) await caches.delete(key);
  await self.registration.unregister();
})()));
"""


def _headers(content_type: str, length: int) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", content_type.encode()),
        (b"content-length", str(length).encode()),
        (b"cache-control", b"no-cache, must-revalidate"),
    ]


def wrap(app: ASGIApp, skin_dir: Path, platform: str) -> ASGIApp:
    skin_dir = skin_dir.resolve()
    # Stylesheets in <head> block the first paint, so the room macos.css makes
    # for the window buttons is there on the very first frame.
    tags = [
        f'<meta name="simplixity-desktop" content="{platform}">',
        f'<link rel="stylesheet" href="{PREFIX}skin.css">',
    ]
    if (skin_dir / f"{platform}.css").is_file():
        tags.append(f'<link rel="stylesheet" href="{PREFIX}{platform}.css">')
    tags.append(f'<script src="{PREFIX}skin.js" defer></script>')
    head = "".join(tags).encode()

    async def respond(send: Send, status: int, body: bytes, content_type: str) -> None:
        await send({"type": "http.response.start", "status": status, "headers": _headers(content_type, len(body))})
        await send({"type": "http.response.body", "body": body})

    async def asgi(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await app(scope, receive, send)

        path: str = scope["path"]
        if path.startswith(PREFIX):
            file = (skin_dir / path[len(PREFIX):]).resolve()
            if file.is_file() and file.is_relative_to(skin_dir):
                ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                return await respond(send, 200, file.read_bytes(), ctype)
            return await respond(send, 404, b"not found", "text/plain")
        if path == "/sw.js":
            return await respond(send, 200, KILL_WORKER, "text/javascript")

        # Compression would hide `</head>` from the rewrite below, and on a
        # loopback socket it only costs time.
        scope = {**scope, "headers": [(k, v) for k, v in scope["headers"] if k != b"accept-encoding"]}

        start: dict | None = None
        chunks: list[bytes] = []

        async def send_wrapper(message: dict) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                ctype = dict(message.get("headers", [])).get(b"content-type", b"")
                if ctype.startswith(b"text/html"):
                    start = message  # hold it: the length changes
                    return
                return await send(message)
            if message["type"] == "http.response.body" and start is not None:
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(chunks)
                if b"</head>" in body:
                    body = body.replace(b"</head>", head + b"</head>", 1)
                # The validators describe upstream's file, not this page: a 304
                # against them would keep an old skin after an app update.
                dropped = (b"content-length", b"etag", b"last-modified")
                headers = [(k, v) for k, v in start.get("headers", []) if k not in dropped]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                return await send({"type": "http.response.body", "body": body})
            await send(message)

        await app(scope, receive, send_wrapper)

    return asgi
