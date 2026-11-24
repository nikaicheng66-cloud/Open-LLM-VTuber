"""[deskpet] Only the pet's own windows may talk to this server from a browser.

The server listens on localhost, but any web page open in the user's browser can still
send requests to localhost (and open WebSockets, which CORS does not cover at all). Since
text sent to the pet can start a Claude Code task that runs commands, a page must not be
able to do that. Browsers always send an Origin header, so:

  - no Origin header        -> a local program (curl hooks, the phone remote relay): allowed
  - Origin is the pet itself -> the Electron client (file://) or the web UI on this server: allowed
  - any other Origin         -> rejected (403 for HTTP, closed for WebSocket)

"null" is not the pet (2026-11-22): any web page gets it with a sandboxed iframe, and the Electron
client was measured to send file:// on both WebSocket and HTTP. Neither is localhost on another
port: that is whatever else happens to run on this PC. A dev server (npm run dev) is refused too.
"""

import re

from loguru import logger

OWN_PORT = 12393  # as in conf.yaml, the launcher, the client and the phone relay
ALLOWED_ORIGIN_RE = re.compile(rf"^(file://|http://(localhost|127\.0\.0\.1|\[::1\]):{OWN_PORT})\Z", re.I)


def origin_allowed(origin: str | None) -> bool:
    return origin is None or bool(ALLOWED_ORIGIN_RE.match(origin))


class OriginGuard:
    """Pure ASGI middleware, so it covers WebSocket handshakes as well as HTTP."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            origin = None
            for k, v in scope.get("headers") or []:
                if k == b"origin":
                    origin = v.decode("latin-1")
                    break
            if not origin_allowed(origin):
                logger.warning(f"[deskpet] blocked {scope['type']} {scope.get('path')} from origin {origin!r}")
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
                await send({"type": "http.response.body", "body": b"forbidden origin"})
                return
        await self.app(scope, receive, send)
