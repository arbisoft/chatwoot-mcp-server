"""Each signed-in user's own Chatwoot key, fetched from their SSO identity through Chatwoot's own SSO handoff.

Chatwoot gives every user one permanent personal key and has no API to mint a narrower or expiring one, so
"minting" here means fetching that key the way the browser login does. No Platform app token is needed.

Bootstrap (cache miss, re-check, or a rejected key), always on a fresh httpx client of its own and the internal
Chatwoot URL, which the public SSO proxy does not front:

1. ``GET /auth/sso/proxy-login`` with ``X-Auth-Request-Email`` (plus ``X-Auth-Request-Access-Token`` for the
   corporate-ID gate, when the session has one). Chatwoot (``AUTH_TYPE=SSO``) resolves or provisions the user,
   joins them to the account as an agent, and answers ``302`` to ``/app/login?email=...&sso_auth_token=...`` with a
   five-minute, single-use handoff token. A 404 means SSO mode is off; a 403 means the corporate gate refused.
2. ``POST /auth/sign_in`` with that email and token. Chatwoot answers with the user, including their personal
   ``access_token``, and opens a devise_token_auth session (``access-token``, ``client``, ``uid`` headers).
3. Always, even when an earlier step failed or the request was cancelled (shielded, for at most five seconds):
   ``DELETE /auth/sign_out`` with those headers, so the session this opened does not linger.
4. ``GET /api/v1/profile`` with the key: the user's role in the configured account. No membership fails closed.

The key never revokes or resets: it is the same key the user may use elsewhere. The result is cached
Fernet-encrypted under the deployment namespace and ``sha256(identity)`` for a week, and re-checked through the
full handoff every four hours, so a user removed from the corporate ID or from the account loses access within that
time. A role-gated refusal only sets a "due" flag behind a ``SET NX EX`` limiter (at most once per five minutes per
identity). A 401 (the user reset their key) drops the entry and fetches again. Every fetch, re-check and denial
writes one ``chatwoot_mcp.audit`` line, without the key or the email.

Synthetic identities are refused before any request, because the first request would create that user in Chatwoot.
Every other failure fails closed: there is no fallback to another key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse

import anyio
import httpx
from cryptography.fernet import Fernet, InvalidToken

from chatwoot_mcp.client.errors import ChatwootError, ChatwootTransportError, error_detail, error_for_status
from chatwoot_mcp.client.http import TOKEN_HEADER, USER_AGENT, cookieless_jar, is_valid_token
from chatwoot_mcp.client.models import Profile as ChatwootProfile
from chatwoot_mcp.credentials.base import PROFILE_PATH, profile_from
from chatwoot_mcp.platform.identity import Identity
from chatwoot_mcp.platform.storage import KeyValue
from chatwoot_mcp.tiers import Tier

logger = logging.getLogger(__name__)
audit = logging.getLogger("chatwoot_mcp.audit")

EMAIL_HEADER = "X-Auth-Request-Email"
ACCESS_TOKEN_HEADER = "X-Auth-Request-Access-Token"
PROXY_LOGIN_PATH = "/auth/sso/proxy-login"
SIGN_IN_PATH = "/auth/sign_in"
SIGN_OUT_PATH = "/auth/sign_out"
SESSION_HEADERS = ("access-token", "client", "uid")
SIGN_OUT_TIMEOUT_SECONDS = 5.0
CACHE_TTL_SECONDS = 7 * 24 * 3600
RECHECK_INTERVAL_SECONDS = 4 * 3600
FORBIDDEN_RECHECK_INTERVAL_SECONDS = 5 * 60
CACHE_KEY_PREFIX = "chatwoot-mcp:key:v1:"
RECHECK_LIMIT_KEY_PREFIX = "chatwoot-mcp:recheck-limit:v1:"
RECHECK_DUE_KEY_PREFIX = "chatwoot-mcp:recheck-due:v1:"
CACHE_SALT = "chatwoot-mcp-key-cache"
DEFAULT_NAMESPACE = "local"

Clock = Callable[[], float]


class SyntheticIdentityError(ChatwootError):
    def __init__(self) -> None:
        super().__init__(
            "your account has no verified email address yet; verify your email in the portal first, "
            "then reconnect this MCP server"
        )


class HandoffDeniedError(ChatwootError):
    """Chatwoot refused this identity: the corporate-ID gate, or an error from its SSO handoff."""


class NotAMemberError(ChatwootError):
    def __init__(self) -> None:
        super().__init__("your Chatwoot user is not a member of the account this server works in")


class MintError(ChatwootError):
    """Chatwoot did not hand out a usable key."""


class TokenCacheError(ChatwootError):
    """The key cache could not be read or written; the request fails closed."""


@dataclass(frozen=True)
class MintedToken:
    token: str = field(repr=False)
    permissions: frozenset[str]
    tier: Tier
    checked_at: float

    def due_for_recheck(self, now: float) -> bool:
        return now - self.checked_at >= RECHECK_INTERVAL_SECONDS

    def to_json(self) -> str:
        return json.dumps(
            {
                "token": self.token,
                "permissions": sorted(self.permissions),
                "tier": self.tier.name,
                "checked_at": self.checked_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> MintedToken:
        data = json.loads(raw)
        return cls(
            token=data["token"],
            permissions=frozenset(data["permissions"]),
            tier=Tier[data["tier"]],
            checked_at=float(data["checked_at"]),
        )


@dataclass(frozen=True)
class CacheEntry:
    minted: MintedToken | None
    recheck_requested: bool = False

    def due_for_recheck(self, now: float) -> bool:
        return self.minted is not None and (self.recheck_requested or self.minted.due_for_recheck(now))


class TokenCache:
    """Encrypted key cache plus the 403 re-check markers, all under one deployment namespace."""

    def __init__(self, store: KeyValue, fernet: Fernet, *, namespace: str = DEFAULT_NAMESPACE) -> None:
        self._store = store
        self._fernet = fernet
        self._namespace = namespace

    def token_key(self, identity_key: str) -> str:
        return f"{CACHE_KEY_PREFIX}{self._namespace}:{identity_key}"

    def limit_key(self, identity_key: str) -> str:
        return f"{RECHECK_LIMIT_KEY_PREFIX}{self._namespace}:{identity_key}"

    def due_key(self, identity_key: str) -> str:
        return f"{RECHECK_DUE_KEY_PREFIX}{self._namespace}:{identity_key}"

    async def load(self, identity_key: str) -> CacheEntry:
        try:
            raw = await self._store.get(self.token_key(identity_key))
            due = await self._store.get(self.due_key(identity_key))
        except Exception as exc:
            raise TokenCacheError("the key cache could not be read; try again shortly") from exc
        return CacheEntry(self._decode(raw), recheck_requested=due is not None)

    def _decode(self, raw: str | None) -> MintedToken | None:
        if raw is None:
            return None
        try:
            return MintedToken.from_json(self._fernet.decrypt(raw.encode()).decode())
        except (InvalidToken, ValueError, KeyError):
            # A rotated secret or an old format: treat as absent and fetch again.
            return None

    async def put(self, identity_key: str, minted: MintedToken) -> None:
        sealed = self._fernet.encrypt(minted.to_json().encode()).decode()
        try:
            await self._store.set(self.token_key(identity_key), sealed, CACHE_TTL_SECONDS)
        except Exception as exc:
            raise TokenCacheError("the key cache could not be written; try again shortly") from exc

    async def drop(self, identity_key: str) -> None:
        try:
            await self._store.delete(self.token_key(identity_key))
        except Exception as exc:
            raise TokenCacheError("the key cache could not be updated; try again shortly") from exc

    async def request_recheck(self, identity_key: str) -> bool:
        """Mark a re-check as due, at most once per five minutes per identity; true when this call marked it."""
        if not await self._store.set_if_absent(self.limit_key(identity_key), "1", FORBIDDEN_RECHECK_INTERVAL_SECONDS):
            return False
        await self._store.set(self.due_key(identity_key), "1", CACHE_TTL_SECONDS)
        return True

    async def clear_recheck(self, identity_key: str) -> None:
        await self._store.delete(self.due_key(identity_key))


def _bootstrap_error(response: httpx.Response) -> ChatwootError:
    return error_for_status(response.status_code, error_detail(response))


def _session_headers(response: httpx.Response) -> dict[str, str] | None:
    headers = {name: response.headers.get(name, "") for name in SESSION_HEADERS}
    return headers if all(headers.values()) else None


class KeySession:
    """One bootstrap: the SSO handoff, sign-in, sign-out and profile read, on its own short-lived client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._signed_in: dict[str, str] | None = None

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ChatwootTransportError(f"could not reach Chatwoot ({type(exc).__name__})") from exc

    async def handoff(self, identity: Identity) -> tuple[str, str]:
        """The email Chatwoot resolved and its single-use ``sso_auth_token``."""
        headers = {EMAIL_HEADER: identity.email}
        if identity.access_token:
            headers[ACCESS_TOKEN_HEADER] = identity.access_token
        response = await self._send("GET", PROXY_LOGIN_PATH, headers=headers)
        if response.status_code == 404:
            raise MintError("Chatwoot's SSO handoff is not available; Chatwoot must run with AUTH_TYPE=SSO")
        if response.status_code == 403:
            raise HandoffDeniedError("Chatwoot refused this account at the corporate-ID gate (HTTP 403)")
        if not response.is_redirect:
            raise _bootstrap_error(response)
        query = parse_qs(urlparse(response.headers.get("location", "")).query)
        token = (query.get("sso_auth_token") or [""])[0]
        email = (query.get("email") or [""])[0]
        if not token or not email:
            reason = (query.get("error") or ["no handoff token"])[0]
            raise HandoffDeniedError(f"Chatwoot refused the sign-in ({reason})")
        return email, token

    async def sign_in(self, email: str, sso_token: str) -> str:
        response = await self._send("POST", SIGN_IN_PATH, json={"email": email, "sso_auth_token": sso_token})
        if not response.is_success:
            raise _bootstrap_error(response)
        self._signed_in = _session_headers(response)
        try:
            data = response.json().get("data") or {}
        except (ValueError, AttributeError):
            data = {}
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or not is_valid_token(token):
            raise MintError("Chatwoot did not return a usable personal key for this user")
        return token

    async def sign_out(self) -> bool:
        if self._signed_in is None:
            return False
        try:
            response = await self._client.delete(SIGN_OUT_PATH, headers=self._signed_in)
        except httpx.HTTPError:
            logger.warning("could not sign out the bootstrap session")
            return False
        return response.is_success

    async def profile(self, token: str) -> ChatwootProfile:
        response = await self._send("GET", PROFILE_PATH, headers={TOKEN_HEADER: token})
        if not response.is_success:
            raise _bootstrap_error(response)
        return ChatwootProfile.parse(response.json())


class TokenEndpoint:
    """Opens a :class:`KeySession` on a fresh, cookieless client that never follows redirects."""

    def __init__(
        self, base_url: str, *, timeout: httpx.Timeout, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport

    @asynccontextmanager
    async def session(self) -> AsyncIterator[KeySession]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            cookies=cookieless_jar(),
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            follow_redirects=False,
            trust_env=False,
        ) as client:
            session = KeySession(client)
            try:
                yield session
            finally:
                with anyio.move_on_after(SIGN_OUT_TIMEOUT_SECONDS, shield=True):
                    await session.sign_out()


class KeyedLocks:
    """One lock per identity, dropped when nobody holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


def _audit(event: str, identity: Identity, minted: MintedToken | None = None, **details: str) -> None:
    entry: dict[str, Any] = {"event": event, "identity": identity.short_key, **details}
    if minted is not None:
        entry["tier"] = minted.tier.name.lower()
        entry["permissions"] = sorted(minted.permissions)
    audit.info(json.dumps(entry))


class TokenMinter:
    def __init__(
        self,
        endpoint: TokenEndpoint,
        cache: TokenCache,
        *,
        account_id: int,
        default_email_domain: str,
        clock: Clock = time.time,
    ) -> None:
        self._endpoint = endpoint
        self._cache = cache
        self._account_id = account_id
        self._default_email_domain = default_email_domain
        self._clock = clock
        self._locks = KeyedLocks()

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    def _refuse_synthetic(self, identity: Identity) -> None:
        if identity.is_synthetic(self._default_email_domain):
            _audit("key.refused", identity, reason="synthetic")
            raise SyntheticIdentityError()

    async def token_for(self, identity: Identity) -> MintedToken:
        self._refuse_synthetic(identity)
        entry = await self._cache.load(identity.key)
        if entry.minted is not None and not entry.due_for_recheck(self._clock()):
            return entry.minted
        async with self._locks.hold(identity.key):
            entry = await self._cache.load(identity.key)
            if entry.minted is None:
                return await self._mint(identity, reason="missing")
            if entry.due_for_recheck(self._clock()):
                return await self._recheck(identity, entry.minted)
            return entry.minted

    async def renew(self, identity: Identity, rejected_token: str) -> MintedToken:
        """After a 401: fetch again, unless another request already replaced the rejected key."""
        self._refuse_synthetic(identity)
        async with self._locks.hold(identity.key):
            entry = await self._cache.load(identity.key)
            if entry.minted is not None and entry.minted.token != rejected_token:
                _audit("key.renew_reused", identity, entry.minted)
                return entry.minted
            await self._cache.drop(identity.key)
            return await self._mint(identity, reason="rejected")

    async def note_forbidden(self, identity: Identity) -> bool:
        requested = await self._cache.request_recheck(identity.key)
        if requested:
            _audit("key.recheck_requested", identity)
        return requested

    async def _bootstrap(self, identity: Identity) -> MintedToken:
        async with self._endpoint.session() as session:
            email, sso_token = await session.handoff(identity)
            token = await session.sign_in(email, sso_token)
            profile = profile_from(await session.profile(token), self._account_id)
        if profile.tier is None or profile.tier == Tier.NONE:
            raise NotAMemberError()
        return MintedToken(token=token, permissions=profile.permissions, tier=profile.tier, checked_at=self._clock())

    async def _fetch(self, identity: Identity) -> MintedToken:
        try:
            return await self._bootstrap(identity)
        except (HandoffDeniedError, NotAMemberError) as error:
            await self._cache.drop(identity.key)
            _audit("key.denied", identity, reason=type(error).__name__)
            raise

    async def _mint(self, identity: Identity, *, reason: str) -> MintedToken:
        minted = await self._fetch(identity)
        await self._store(identity, minted)
        _audit("key.fetched", identity, minted, reason=reason)
        return minted

    async def _recheck(self, identity: Identity, cached: MintedToken) -> MintedToken:
        minted = await self._fetch(identity)
        await self._store(identity, minted)
        unchanged = minted.token == cached.token and minted.permissions == cached.permissions
        _audit("key.recheck_unchanged" if unchanged else "key.fetched", identity, minted, reason="recheck")
        return minted

    async def _store(self, identity: Identity, minted: MintedToken) -> None:
        await self._cache.put(identity.key, minted)
        try:
            await self._cache.clear_recheck(identity.key)
        except Exception:
            logger.warning("could not clear the re-check flag", exc_info=True)
