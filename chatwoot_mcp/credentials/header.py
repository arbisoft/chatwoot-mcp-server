"""Hosted mode: every caller sends their own Chatwoot key as `X-Chatwoot-Token`.

The server is mounted at `/http/api-key/mcp`. The mount strips every `X-Auth-Request-*` header on arrival,
because those carry identity inside Moneta and must only ever come from the front door. Nothing from the
caller other than the key is used.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_http_request
from starlette.types import ASGIApp, Receive, Scope, Send

API_KEY_MOUNT_PATH = "/http/api-key"
TOKEN_HEADER = "x-chatwoot-token"


def is_identity_header(name: str) -> bool:
    lowered = name.lower().replace("_", "-")
    return lowered.startswith("x-auth-request-")


def api_key_mount(app: ASGIApp) -> ASGIApp:
    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            kept = [(n, v) for n, v in scope.get("headers", []) if not is_identity_header(n.decode("latin-1"))]
            scope = {**scope, "headers": kept}
        await app(scope, receive, send)

    return wrapped


def request_token() -> str | None:
    """The key the current HTTP caller sent, or None when there is no HTTP request or no header."""
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    return request.headers.get(TOKEN_HEADER, "").strip() or None
