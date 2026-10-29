"""Async Chatwoot REST client: the only code that talks to Chatwoot.

- One shared ``httpx.AsyncClient`` per process, created lazily so it binds to the running event loop, and dropped
  again by :meth:`ChatwootClient.aclose`.
- The key travels per call in Chatwoot's own ``api_access_token`` header; the shared client carries no credential
  of its own. It never appears in an error: Chatwoot's error text is scrubbed of it.
- Cookies are refused outright, so no session can leak between users. The refusing ``CookieJar`` is handed to
  httpx as is: wrapping it in ``httpx.Cookies()`` copies the cookies into a fresh jar and drops the refusal.
- A request hook makes it impossible to send any ``X-Auth-Request-*`` header, which an SSO-fronted Chatwoot would
  accept as proof of identity.
- Redirects are never followed, so a key cannot be handed to another host.
- ``trust_env=False``: no proxy or ``.netrc`` from the environment ever sees the key.
- Retries (see :class:`RetryPolicy`): GET, PUT and DELETE retry on connect errors, read and write failures,
  protocol errors and 502/503/504. POST retries only when the request provably never left the process
  (``ConnectError``, ``ConnectTimeout``, ``PoolTimeout``), so a reply is never sent twice. A 429 is retried once
  for every method, after ``Retry-After``, because Chatwoot did not process it.
- An optional ``on_rejected`` hook sees every 401 and 403. When it returns a different key the request is sent once
  more with it (a 401 means Chatwoot did not process the request, so this is safe for POST too). Platform mode uses
  it to fetch the key again after the user reset theirs.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar, CookiePolicy
from typing import Any

import httpx

from chatwoot_mcp.client.errors import (
    AuthError,
    ChatwootAPIError,
    ChatwootError,
    ChatwootTransportError,
    IdentityHeaderError,
    InvalidTokenError,
    MissingTokenError,
    PermissionDenied,
    ServerError,
    error_detail,
    error_for_status,
)

IDENTITY_HEADER_PREFIX = "x-auth-request-"
TOKEN_HEADER = "api_access_token"
USER_AGENT = "chatwoot-mcp-server"
REDACTED = "[key hidden]"
RETRY_SAFE_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE"})
RETRYABLE_STATUSES = frozenset({502, 503, 504})
PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
IN_FLIGHT_ERRORS = (httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.RemoteProtocolError)

Sleep = Callable[[float], Awaitable[None]]
RejectionHook = Callable[[ChatwootAPIError, str], Awaitable[str | None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay_seconds: float = 0.25
    rate_limit_cap_seconds: float = 10.0
    rate_limit_default_seconds: float = 1.0

    def backoff(self, attempt: int, rng: random.Random) -> float:
        delay = self.base_delay_seconds * 2**attempt
        return delay + rng.uniform(0, delay)

    def rate_limit_wait(self, response: httpx.Response) -> float:
        raw = response.headers.get("retry-after", "").strip()
        seconds = float(raw) if raw.isascii() and raw.isdigit() else self.rate_limit_default_seconds
        return min(seconds, self.rate_limit_cap_seconds)


class _RefuseAllCookies(CookiePolicy):
    netscape = True
    rfc2965 = False
    hide_cookie2 = True

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False

    def domain_return_ok(self, domain, request) -> bool:
        return False

    def path_return_ok(self, path, request) -> bool:
        return False


def cookieless_jar() -> CookieJar:
    return CookieJar(policy=_RefuseAllCookies())


def is_identity_header(name: str) -> bool:
    """``X-Auth-Request-*`` in any case, with ``_`` treated as ``-`` (some proxies map one to the other)."""
    return name.lower().replace("_", "-").startswith(IDENTITY_HEADER_PREFIX)


def is_valid_token(token: str) -> bool:
    return token.isascii() and token.isprintable() and " " not in token


async def reject_identity_headers(request: httpx.Request) -> None:
    leaked = [name for name in request.headers if is_identity_header(name)]
    if leaked:
        raise IdentityHeaderError(f"refusing to send identity header(s) {', '.join(sorted(leaked))} to Chatwoot")


def never_sent(error: ChatwootError) -> bool:
    """The request provably never left the process, so Chatwoot did nothing."""
    return isinstance(error, ChatwootTransportError) and isinstance(error.__cause__, PRE_SEND_ERRORS)


def outcome_unknown(error: ChatwootError) -> bool:
    """Chatwoot may or may not have acted: the connection failed mid-request, or a 5xx came back."""
    return isinstance(error, ServerError) or (isinstance(error, ChatwootTransportError) and not never_sent(error))


def accepted_but_unreadable(error: ChatwootError) -> bool:
    """Chatwoot answered 2xx, so it acted, but the body could not be decoded."""
    return isinstance(error, ChatwootAPIError) and error.status_code < 300


def query(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop ``None`` values; send lists the Rails way, as ``key[]``."""
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, list | tuple):
            out[f"{key}[]"] = [str(item) for item in value]
        else:
            out[key] = value
    return out


def _json_body(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise ChatwootAPIError(response.status_code, "response was not JSON") from exc


def _retryable_transport_error(error: httpx.TransportError, retry_safe: bool) -> bool:
    if isinstance(error, PRE_SEND_ERRORS):
        return True
    return retry_safe and isinstance(error, IN_FLIGHT_ERRORS)


def _scrubbed(error: ChatwootAPIError, token: str) -> ChatwootAPIError:
    """Chatwoot's own error text is shown to the user, so make sure it can never carry the key."""
    if token not in error.detail:
        return error
    return type(error)(error.status_code, error.detail.replace(token, REDACTED))


class ChatwootClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 30.0,
        connect_timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        retry: RetryPolicy | None = None,
        sleep: Sleep | None = None,
        rng: random.Random | None = None,
        on_rejected: RejectionHook | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        self._transport = transport
        self._retry = retry or RetryPolicy()
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._on_rejected = on_rejected
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                cookies=cookieless_jar(),
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                event_hooks={"request": [reject_identity_headers]},
                follow_redirects=False,
                trust_env=False,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        retry_safe: bool | None = None,
    ) -> Any:
        """Send one request and return its decoded JSON body (``None`` when empty).

        ``retry_safe`` overrides the method default, for read-only POSTs such as ``/conversations/filter``.
        """
        response = await self._send(method, path, token=token, params=params, json=json, retry_safe=retry_safe)
        return _json_body(response)

    async def get(self, path: str, *, token: str | None, params: Mapping[str, Any] | None = None) -> Any:
        return await self.request("GET", path, token=token, params=params)

    async def _send(self, method: str, path: str, *, token: str | None, **options: Any) -> httpx.Response:
        try:
            return await self._attempt(method, path, token=token, **options)
        except (AuthError, PermissionDenied) as error:
            replacement = await self._on_rejected(error, token) if self._on_rejected and token else None
            if not replacement or replacement == token:
                raise
        return await self._attempt(method, path, token=replacement, **options)

    async def _attempt(
        self,
        method: str,
        path: str,
        *,
        token: str | None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        retry_safe: bool | None = None,
    ) -> httpx.Response:
        if not token:
            raise MissingTokenError("no Chatwoot key is configured for this request")
        if not is_valid_token(token):
            raise InvalidTokenError("the Chatwoot key must be printable ASCII without spaces")
        method = method.upper()
        safe = method in RETRY_SAFE_METHODS if retry_safe is None else retry_safe
        client = self._client()
        attempt = 0
        rate_limit_retried = False
        while True:
            request = client.build_request(method, path, params=query(params), json=json, headers={TOKEN_HEADER: token})
            try:
                response = await client.send(request)
            except httpx.TransportError as exc:
                if attempt < self._retry.max_retries and _retryable_transport_error(exc, safe):
                    await self._sleep(self._retry.backoff(attempt, self._rng))
                    attempt += 1
                    continue
                raise ChatwootTransportError(f"could not reach Chatwoot ({type(exc).__name__})") from exc

            if response.status_code == 429 and not rate_limit_retried:
                rate_limit_retried = True
                await self._sleep(self._retry.rate_limit_wait(response))
                continue
            if response.status_code in RETRYABLE_STATUSES and safe and attempt < self._retry.max_retries:
                await self._sleep(self._retry.backoff(attempt, self._rng))
                attempt += 1
                continue
            if not response.is_success:
                raise _scrubbed(error_for_status(response.status_code, error_detail(response)), token)
            return response
