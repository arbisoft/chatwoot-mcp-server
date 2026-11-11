"""Where each tool call's Chatwoot key comes from, and the key owner's role.

- :class:`StaticCredentialProvider`: one key from ``CHATWOOT_HTTP_TOKEN`` (stdio, or ``/mcp`` with
  ``CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true``). Its tier is read once and kept for the process.
- :class:`HeaderCredentialProvider`: each caller's own ``X-Chatwoot-Token`` on ``/http/api-key/mcp``. The mount
  strips ``X-Auth-Request-*`` headers on arrival, so a caller can never smuggle an SSO identity to Chatwoot.
- Platform mode has its own provider in ``platform/credentials.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from fastmcp.server.dependencies import get_http_request
from starlette.types import ASGIApp, Receive, Scope, Send

from chatwoot_mcp.client import ChatwootClient, ChatwootError
from chatwoot_mcp.client.errors import InvalidTokenError, MissingTokenError
from chatwoot_mcp.client.http import is_identity_header, is_valid_token
from chatwoot_mcp.client.models import Profile as ChatwootProfile
from chatwoot_mcp.tiers import Tier, tier_for

IDENTITY_LENGTH = 16
PROFILE_PATH = "/api/v1/profile"

Clock = Callable[[], float]


def token_identity(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:IDENTITY_LENGTH]


@dataclass(frozen=True)
class Profile:
    """What the key's owner may do in the configured account, read from their Chatwoot profile."""

    tier: Tier | None = None
    permissions: frozenset[str] = frozenset()

    @property
    def known(self) -> bool:
        return self.tier is not None


UNKNOWN_PROFILE = Profile()


@dataclass(frozen=True)
class ChatwootCredential:
    token: str = field(repr=False)
    identity: str
    tier: Tier | None
    permissions: frozenset[str]

    @classmethod
    def for_token(cls, token: str, profile: Profile) -> ChatwootCredential:
        return cls(token=token, identity=token_identity(token), tier=profile.tier, permissions=profile.permissions)


class CredentialProvider(Protocol):
    async def resolve(self) -> ChatwootCredential:
        """Return the credential for the current request, or raise a :class:`ChatwootError`."""
        ...


def profile_from(profile: ChatwootProfile, account_id: int) -> Profile:
    account = profile.account(account_id)
    if account is None:
        return Profile(tier=tier_for(None))
    # Older Chatwoot lists no permissions, only the role, which names the same two built-in roles.
    held = account.permissions or ([account.role] if account.role else [])
    return Profile(tier=tier_for(held), permissions=frozenset(held))


async def fetch_profile(client: ChatwootClient, token: str, account_id: int) -> Profile:
    """The key owner's role in the configured account, from ``GET /api/v1/profile``."""
    return profile_from(ChatwootProfile.parse(await client.get(PROFILE_PATH, token=token)), account_id)


ProfileLoader = Callable[[], Awaitable[Profile]]


class ProfileMemo:
    """In-process memo of profiles, keyed by credential identity.

    Holds at most ``max_entries`` identities and evicts the least recently stored first. A failed lookup is
    remembered as unknown for ``failure_ttl`` seconds so a broken lookup costs one extra request a minute, not one
    per call. The memo is the one deliberately mutable object here: it is a cache.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float | None,
        failure_ttl_seconds: float = 60.0,
        max_entries: int = 1024,
        clock: Clock = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._failure_ttl = failure_ttl_seconds
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: OrderedDict[str, tuple[Profile, float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def _fresh(self, identity: str) -> Profile | None:
        entry = self._entries.get(identity)
        if entry is None or entry[1] <= self._clock():
            return None
        return entry[0]

    def _store(self, identity: str, profile: Profile) -> None:
        ttl = self._ttl if profile.known else self._failure_ttl
        self._entries[identity] = (profile, float("inf") if ttl is None else self._clock() + ttl)
        self._entries.move_to_end(identity)
        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            self._release_idle_lock(evicted)

    def _release_idle_lock(self, identity: str) -> None:
        lock = self._locks.get(identity)
        if lock is not None and not lock.locked():
            del self._locks[identity]

    async def get(self, identity: str, load: ProfileLoader) -> Profile:
        cached = self._fresh(identity)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(identity, asyncio.Lock())
        try:
            async with lock:
                cached = self._fresh(identity)
                if cached is not None:
                    return cached
                profile = await _load_or_unknown(load)
                self._store(identity, profile)
                return profile
        finally:
            if identity not in self._entries:
                self._release_idle_lock(identity)


async def _load_or_unknown(load: ProfileLoader) -> Profile:
    try:
        return await load()
    except ChatwootError:
        return UNKNOWN_PROFILE


class StaticCredentialProvider:
    def __init__(
        self, client: ChatwootClient, token: str | None, account_id: int, *, memo: ProfileMemo | None = None
    ) -> None:
        self._client = client
        self._token = token
        self._account_id = account_id
        self._memo = memo if memo is not None else ProfileMemo(ttl_seconds=None)

    async def resolve(self) -> ChatwootCredential:
        if not self._token:
            raise MissingTokenError("no Chatwoot key is configured for this request (set CHATWOOT_HTTP_TOKEN)")
        token = self._token
        profile = await self._memo.get(
            token_identity(token), lambda: fetch_profile(self._client, token, self._account_id)
        )
        return ChatwootCredential.for_token(token, profile)

    async def warm(self) -> None:
        if self._token:
            await self.resolve()


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
