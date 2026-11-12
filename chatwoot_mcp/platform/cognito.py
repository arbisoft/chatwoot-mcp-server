"""Cognito provider that keeps the upstream id_token's identity for every request.

FastMCP's ``AWSCognitoProvider`` validates the Cognito access token, which
carries no ``email`` and, for federated users, an opaque ``username``. The
mint needs the identity the browser login uses, so this provider:

- seals nothing into the JWTs it issues to MCP clients: they are signed, not
  encrypted, and a sealed id_token would let anyone holding a client's bearer
  replay it at the SSO edge;
- reads the id_token, and the raw upstream access token for the corporate-ID
  gate, from the upstream token store on every request and fails closed when it
  is missing (:meth:`load_access_token`);
- raises when that store is down, so an outage is a server error rather than a
  401 that sends every client through a new login;
- corrects the upstream ``expires_in`` on both grants
  (:func:`_correct_expires_in`).

The id_token's signature and audience are verified (:class:`IdTokenVerifier`) against Cognito's JWKS, or, with
``LAUNCHPAD_EMAIL_CAPTURE`` on, against mpass-auth-proxy's, which re-signs it with the overlaid email; and its
``sub`` must match the JWKS-verified access token's. So a token endpoint that is answered by anyone else, or an
id_token for another user, is rejected. Its ``exp`` is not checked: the access token, verified on every request,
carries the session's freshness.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.aws import AWSCognitoProvider
from joserfc.errors import JoseError

logger = logging.getLogger(__name__)

UPSTREAM_CLAIMS_KEY = "upstream_claims"
ID_TOKEN_KEY = "id_token"
EMAIL_CLAIM = "email"
EMAIL_VERIFIED_CLAIM = "email_verified"
COGNITO_USERNAME_CLAIM = "cognito:username"
PREFERRED_USERNAME_CLAIM = "preferred_username"
ACCESS_TOKEN_CLAIM = "access_token"

STORE_PROBE_KEY = "chatwoot-mcp-store-probe"

SUB_CLAIM = "sub"
# A key we have not seen is looked up again at most this often, so a forged kid cannot make every request fetch.
JWKS_REFETCH_SECONDS = 60

MIN_EXPIRES_IN_SECONDS = 60
EXPIRES_IN_DRIFT_TOLERANCE_SECONDS = 1


def _true_expires_in(token_response: dict[str, Any]) -> int | None:
    """The upstream access token's real remaining lifetime, from its own ``exp``.

    mpass-auth-proxy reports the browser cookie lifetime (days) as
    ``expires_in`` while the Cognito token expires in an hour. FastMCP gates
    its transparent refresh on that value, so left alone the refresh never
    fires and the client is sent through a full re-authorization hourly.
    The floor keeps an already-expired token from yielding a negative TTL.
    """
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        return None
    try:
        exp = jwt.decode(access_token, options={"verify_signature": False}).get("exp")
    except jwt.PyJWTError as exc:
        logger.warning("could not decode the upstream access_token to read exp: %s", exc)
        return None
    if not isinstance(exp, int | float):
        return None
    return max(int(exp - time.time()), MIN_EXPIRES_IN_SECONDS)


def _already_truthful(reported: Any, corrected: int) -> bool:
    if isinstance(reported, bool) or not isinstance(reported, int | float):
        return False
    return abs(reported - corrected) <= EXPIRES_IN_DRIFT_TOLERANCE_SECONDS


def _correct_expires_in(response: httpx.Response) -> httpx.Response:
    """authlib compliance hook: rewrite ``expires_in`` to the token's real lifetime."""
    if response.status_code != 200:
        return response
    try:
        body = response.json()
    except ValueError:
        return response
    if not isinstance(body, dict):
        return response
    corrected = _true_expires_in(body)
    if corrected is None or _already_truthful(body.get("expires_in"), corrected):
        return response
    return httpx.Response(
        status_code=response.status_code,
        json={**body, "expires_in": corrected},
        request=response.request,
    )


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _field(record: Any, name: str) -> Any:
    if isinstance(record, dict):
        return record.get(name)
    return getattr(record, name, None)


JwksFetcher = Callable[[], Awaitable[dict[str, Any]]]


class IdTokenVerifier:
    """Checks an id_token's RS256 signature against a JWKS, and its audience; keys are cached in process."""

    def __init__(self, fetch: JwksFetcher, audience: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._audience = audience
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None

    @classmethod
    def from_url(cls, url: str, audience: str, *, timeout: float = 10.0) -> IdTokenVerifier:
        async def fetch() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()

        return cls(fetch, audience)

    async def _key(self, kid: str | None) -> Any:
        stale = self._fetched_at is None or self._clock() - self._fetched_at >= JWKS_REFETCH_SECONDS
        if kid not in self._keys and stale:
            self._keys = {key.key_id: key.key for key in jwt.PyJWKSet.from_dict(await self._fetch()).keys}
            self._fetched_at = self._clock()
        return self._keys.get(kid)

    async def claims(self, id_token: str) -> dict[str, Any] | None:
        """The verified claims, or ``None`` for a token that does not verify. A JWKS fetch failure raises."""
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.PyJWTError:
            return None
        key = await self._key(kid)
        if key is None:
            logger.warning("the id_token is signed with an unknown key; rejecting it")
            return None
        try:
            return jwt.decode(
                id_token, key, algorithms=["RS256"], audience=self._audience, options={"verify_exp": False}
            )
        except jwt.PyJWTError as exc:
            logger.warning("the id_token did not verify: %s", exc)
            return None


class ChatwootCognitoProvider(AWSCognitoProvider):
    id_tokens: IdTokenVerifier

    def _create_upstream_oauth_client(self) -> AsyncOAuth2Client:
        """One factory serves both the code exchange and the refresh grant, so one registration covers both."""
        client = super()._create_upstream_oauth_client()
        client.register_compliance_hook("access_token_response", _correct_expires_in)
        client.register_compliance_hook("refresh_token_response", _correct_expires_in)
        return client

    async def _extract_upstream_claims(
        self, idp_tokens: dict[str, Any], *, include_access_token: bool = False
    ) -> dict[str, Any] | None:
        """The caller's identity claims, read per request; ``None`` when FastMCP asks for claims to seal."""
        if not include_access_token:
            return None
        id_token = _string(idp_tokens.get(ID_TOKEN_KEY))
        if id_token is None:
            logger.warning("the Cognito token response has no id_token; the caller's identity is unknown")
            return None
        claims = await self.id_tokens.claims(id_token)
        if claims is None:
            return None
        extracted = {
            ID_TOKEN_KEY: id_token,
            SUB_CLAIM: _string(claims.get(SUB_CLAIM)),
            EMAIL_CLAIM: _string(claims.get(EMAIL_CLAIM)),
            EMAIL_VERIFIED_CLAIM: True if claims.get(EMAIL_VERIFIED_CLAIM) is True else None,
            COGNITO_USERNAME_CLAIM: _string(claims.get(COGNITO_USERNAME_CLAIM)),
            PREFERRED_USERNAME_CLAIM: _string(claims.get(PREFERRED_USERNAME_CLAIM)),
            ACCESS_TOKEN_CLAIM: _string(idp_tokens.get(ACCESS_TOKEN_CLAIM)),
        }
        return {key: value for key, value in extracted.items() if value is not None}

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Attach the current id_token identity, or reject the request when it cannot be found."""
        validated = await super().load_access_token(token)
        if validated is None:
            # The base turns every store error into ``None``; read the store once so an outage raises instead.
            await self._jti_mapping_store.get(key=STORE_PROBE_KEY)
            return None
        upstream = await self._resolve_upstream_claims(token)
        if not upstream or not upstream.get(ID_TOKEN_KEY):
            logger.warning("could not resolve the upstream id_token for this session; rejecting the request")
            return None
        if upstream.get(SUB_CLAIM) != (validated.claims or {}).get(SUB_CLAIM):
            logger.warning("the id_token belongs to another user than the access token; rejecting the request")
            return None
        claims = {**(validated.claims or {}), UPSTREAM_CLAIMS_KEY: upstream}
        return validated.model_copy(update={"claims": claims})

    async def _resolve_upstream_claims(self, token: str) -> dict[str, Any] | None:
        """Follow the issued JWT's jti to the stored Cognito token response; ``None`` on any miss.

        A store failure raises instead, so a Valkey outage is a server error, not a 401 that forces a new login.
        """
        try:
            jti = _field(self.jwt_issuer.verify_token(token), "jti")
        except (JoseError, ValueError) as exc:
            logger.warning("could not verify the issued token: %s", exc)
            return None
        if not jti:
            return None
        mapping = await self._jti_mapping_store.get(key=jti)
        upstream_id = _field(mapping, "upstream_token_id") if mapping else None
        if not upstream_id:
            return None
        token_set = await self._upstream_token_store.get(key=upstream_id)
        raw = _field(token_set, "raw_token_data") if token_set else None
        if not isinstance(raw, dict):
            return None
        return await self._extract_upstream_claims(raw, include_access_token=True)
