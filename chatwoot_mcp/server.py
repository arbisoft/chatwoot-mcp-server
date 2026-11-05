"""Server factories: the FastMCP instance and the HTTP app around it.

The HTTP app serves the MCP endpoint on:

- ``/http/api-key/mcp``, where every caller sends their own key as ``X-Chatwoot-Token`` and ``X-Auth-Request-*``
  headers are stripped, and
- ``/mcp`` with the operator's ``CHATWOOT_HTTP_TOKEN``, only when ``CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true``: anyone
  who reaches it acts as the key's owner.

Two boundary checks run before any tool code, whatever the transport:

- :class:`ArgumentBoundary` refuses tool arguments with control characters (a NUL byte makes Postgres fail, and
  invisible control codes can hide text in a reply), strings over ``MAX_ARGUMENT_CHARS``, and integers outside
  Chatwoot's 32-bit ids. Each tool's schema then checks its own types, ranges and lengths.
- :class:`BodyLimit` refuses HTTP request bodies over ``MAX_BODY_BYTES`` before they are read into memory.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.server.middleware import Middleware
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chatwoot_mcp.client import ChatwootClient
from chatwoot_mcp.config import Settings
from chatwoot_mcp.confirmations import ConfirmationBackend, Confirmations, MemoryConfirmationBackend
from chatwoot_mcp.credentials import (
    API_KEY_MOUNT_PATH,
    CredentialProvider,
    HeaderCredentialProvider,
    MountRoutedCredentialProvider,
    StaticCredentialProvider,
    api_key_mount,
)
from chatwoot_mcp.shaping import UNTRUSTED_NOTICE, Links
from chatwoot_mcp.tiers import TierResolver, community_tier_resolver, require_tier
from chatwoot_mcp.tools import ToolContext, register_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "chatwoot"
MCP_PATH = "/mcp"
# Reading the static key's role at start-up is best effort; it must not keep a stdio client waiting.
WARM_TIMEOUT_SECONDS = 5
MAX_ARGUMENT_CHARS = 10_000
MAX_ID = 2_147_483_647
MAX_BODY_BYTES = 1_048_576
# C0 controls and DEL, except tab, line feed and carriage return, which messages and notes use.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INSTRUCTIONS = (
    "Works with support conversations in Chatwoot, using the caller's own Chatwoot key and permissions. "
    f"{UNTRUSTED_NOTICE} "
    "Tools named prepare_* change nothing: they return a preview and a confirmation_token. Show the preview to the "
    "user, and call the matching tool with the token only after the user clearly agrees."
)


def build_server(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    tier_resolver: TierResolver | None = None,
    client: ChatwootClient | None = None,
    credentials: CredentialProvider | None = None,
    confirmation_backend: ConfirmationBackend | None = None,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Build the community server; platform mode passes its own client, credentials, storage and auth."""
    client = client or ChatwootClient(
        settings.chatwoot_url,
        timeout_seconds=settings.timeout_seconds,
        connect_timeout_seconds=settings.connect_timeout_seconds,
        transport=transport,
    )
    static = StaticCredentialProvider(client, settings.http_token, settings.account_id)
    credentials = credentials or MountRoutedCredentialProvider(
        default=static, api_key=HeaderCredentialProvider(client, settings.account_id)
    )
    resolver = tier_resolver or community_tier_resolver(credentials, filter_by_role=settings.filter_tools_by_role)

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        await _warm(static)
        try:
            yield
        finally:
            await client.aclose()

    context = ToolContext(
        client=client,
        credentials=credentials,
        settings=settings,
        links=Links(settings.browser_url, settings.account_id),
        tier_check=require_tier(resolver),
        confirmations=Confirmations(
            confirmation_backend or MemoryConfirmationBackend(), ttl_seconds=settings.confirm_ttl_seconds
        ),
    )
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, lifespan=lifespan, auth=auth)
    mcp.add_middleware(ArgumentBoundary())
    register_tools(mcp, context, settings)
    return mcp


def argument_problem(value: Any, where: str) -> str | None:
    """Why a tool argument is refused at the boundary, or ``None``; nested lists and objects are checked too."""
    if isinstance(value, str):
        if len(value) > MAX_ARGUMENT_CHARS:
            return f"{where} is longer than {MAX_ARGUMENT_CHARS} characters"
        if _CONTROL_CHARACTERS.search(value):
            return f"{where} contains control characters"
    elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_ID:
        return f"{where} is out of range"
    elif isinstance(value, dict):
        for key, item in value.items():
            problem = argument_problem(key, f"a key in {where}") or argument_problem(item, f"{where}.{key}")
            if problem:
                return problem
    elif isinstance(value, list):
        return next((p for i, item in enumerate(value) if (p := argument_problem(item, f"{where}[{i}]"))), None)
    return None


class ArgumentBoundary(Middleware):
    async def on_call_tool(self, context, call_next):
        for name, value in (context.message.arguments or {}).items():
            problem = argument_problem(value, name)
            if problem:
                raise ToolError(f"Error: {problem}; nothing was sent to Chatwoot")
        return await call_next(context)


class BodyLimit:
    """Refuse bodies over ``MAX_BODY_BYTES``: by ``Content-Length`` up front, or while a chunked body streams."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        length = dict(scope.get("headers", [])).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > self._max_bytes:
            await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"request body too large"})
            return
        received = 0

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            # A chunked body that grows past the limit reads to the app as a client that went away.
            return {"type": "http.disconnect"} if received > self._max_bytes else message

        await self._app(scope, counted, send)


async def _warm(static: StaticCredentialProvider) -> None:
    """Best effort: a Chatwoot that is down or answers oddly must never stop the server starting."""
    try:
        async with asyncio.timeout(WARM_TIMEOUT_SECONDS):
            await static.warm()
    except Exception:
        logger.warning("could not read the static key's Chatwoot role at startup", exc_info=True)


async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_http_app(mcp: FastMCP, settings: Settings) -> Starlette:
    """Serve ``/http/api-key/mcp`` always, and ``/mcp`` only on the shared-token opt-in.

    Raises :class:`ConfigError` for a setup :meth:`Settings.check_http` refuses.
    """
    settings.check_http()
    mcp_app = mcp.http_app(path=MCP_PATH, stateless_http=True)
    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Mount(API_KEY_MOUNT_PATH, app=api_key_mount(mcp_app)),
    ]
    if settings.http_shared_token_route:
        routes.append(Mount("/", app=mcp_app))
    app = Starlette(routes=routes, lifespan=mcp_app.lifespan)
    app.add_middleware(BodyLimit)
    return app
