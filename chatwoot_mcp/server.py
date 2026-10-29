"""Server factories: the FastMCP instance and the HTTP app around it.

The HTTP app serves the MCP endpoint on:

- ``/http/api-key/mcp``, where every caller sends their own key as ``X-Chatwoot-Token`` and ``X-Auth-Request-*``
  headers are stripped, and
- ``/mcp`` with the operator's ``CHATWOOT_HTTP_TOKEN``, only when ``CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true``: anyone
  who reaches it acts as the key's owner.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from chatwoot_mcp.client import ChatwootClient
from chatwoot_mcp.config import Settings
from chatwoot_mcp.confirmations import ConfirmationBackend, Confirmations, MemoryConfirmationBackend
from chatwoot_mcp.credentials import (
    CredentialProvider,
    HeaderCredentialProvider,
    MountRoutedCredentialProvider,
    StaticCredentialProvider,
    api_key_mount,
)
from chatwoot_mcp.credentials.header import API_KEY_MOUNT_PATH
from chatwoot_mcp.shaping import UNTRUSTED_NOTICE, Links
from chatwoot_mcp.tiers import TierResolver, community_tier_resolver, require_tier
from chatwoot_mcp.tools import ToolContext, register_tools

logger = logging.getLogger(__name__)

SERVER_NAME = "chatwoot"
MCP_PATH = "/mcp"
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
    register_tools(mcp, context, settings)
    return mcp


async def _warm(static: StaticCredentialProvider) -> None:
    """Best effort: a Chatwoot that is down or answers oddly must never stop the server starting."""
    try:
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
    return Starlette(routes=routes, lifespan=mcp_app.lifespan)
