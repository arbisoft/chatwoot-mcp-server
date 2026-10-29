"""Community HTTP mode: each caller sends their own key as ``X-Chatwoot-Token``.

Only requests that arrive on the ``/http/api-key`` mount use this provider; :func:`api_key_mount` marks them and
strips every ``X-Auth-Request-*`` header before anything else sees the request, because those carry identity inside
Moneta and must only ever come from the front door. Nothing from the caller other than the key is ever forwarded.

Profiles are memoised for 60 seconds per identity (``sha256(key)[:16]``).
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_http_request
from starlette.types import ASGIApp, Receive, Scope, Send

from chatwoot_mcp.client import ChatwootClient
from chatwoot_mcp.client.errors import InvalidTokenError, MissingTokenError
from chatwoot_mcp.client.http import is_identity_header, is_valid_token
from chatwoot_mcp.credentials.base import (
    ChatwootCredential,
    CredentialProvider,
    ProfileMemo,
    fetch_profile,
    token_identity,
)

API_KEY_MOUNT_PATH = "/http/api-key"
TOKEN_HEADER = "x-chatwoot-token"
HEADER_PROFILE_TTL_SECONDS = 60.0
_SCOPE_MARKER = "chatwoot_mcp.api_key_mount"


def _without_identity_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    return [(name, value) for name, value in headers if not is_identity_header(name.decode("latin-1"))]


def api_key_mount(app: ASGIApp) -> ASGIApp:
    """Wrap the MCP app for the ``/http/api-key`` mount."""

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {**scope, "headers": _without_identity_headers(list(scope.get("headers", []))), _SCOPE_MARKER: True}
        await app(scope, receive, send)

    return wrapped


def _current_scope() -> Scope | None:
    try:
        return get_http_request().scope
    except RuntimeError:
        return None


def on_api_key_mount() -> bool:
    scope = _current_scope()
    return bool(scope and scope.get(_SCOPE_MARKER))


def request_token() -> str | None:
    scope = _current_scope()
    if scope is None:
        return None
    for name, value in scope.get("headers", []):
        if name.lower() == TOKEN_HEADER.encode():
            return value.decode("latin-1").strip() or None
    return None


class HeaderCredentialProvider:
    def __init__(self, client: ChatwootClient, account_id: int, *, memo: ProfileMemo | None = None) -> None:
        self._client = client
        self._account_id = account_id
        self._memo = memo if memo is not None else ProfileMemo(ttl_seconds=HEADER_PROFILE_TTL_SECONDS)

    async def resolve(self) -> ChatwootCredential:
        token = request_token()
        if not token:
            raise MissingTokenError("send your own Chatwoot key in the X-Chatwoot-Token header")
        if not is_valid_token(token):
            raise InvalidTokenError("the X-Chatwoot-Token header must be printable ASCII without spaces")
        profile = await self._memo.get(
            token_identity(token), lambda: fetch_profile(self._client, token, self._account_id)
        )
        return ChatwootCredential.for_token(token, profile)


class MountRoutedCredentialProvider:
    """Header credentials on ``/http/api-key``, the default provider everywhere else."""

    def __init__(self, default: CredentialProvider, api_key: CredentialProvider) -> None:
        self._default = default
        self._api_key = api_key

    async def resolve(self) -> ChatwootCredential:
        provider = self._api_key if on_api_key_mount() else self._default
        return await provider.resolve()
