"""The whole platform path over HTTP: a FastMCP bearer, the stored Cognito token set, the identity, the SSO handoff
to Chatwoot, and the tool call made with that user's own key."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import jwt
import pytest
from fastmcp.server.auth.oauth_proxy.models import JTIMapping, UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from key_value.aio.stores.memory import MemoryStore

from chatwoot_mcp.platform.cognito import _correct_expires_in, _true_expires_in
from chatwoot_mcp.platform.http import build_platform_server
from chatwoot_mcp.platform.settings import PlatformSettings, community_settings
from chatwoot_mcp.platform.storage import MemoryKeyValue
from tests.helpers import MCP_HEADERS
from tests.platform_fakes import FakeSsoChatwoot

ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_test"
CLIENT_ID = "cognito-app-client"
ENV = {
    "MCP_BASE_URL": "https://support-mcp.example.test",
    "COGNITO_USER_POOL_ID": "ap-southeast-1_test",
    "COGNITO_AWS_REGION": "ap-southeast-1",
    "OIDC_CLIENT_ID": CLIENT_ID,
    "OIDC_CLIENT_SECRET": "cognito-app-secret",
    "CHATWOOT_INTERNAL_BASE_URL": "http://chatwoot-web:3000",
    "CHATWOOT_ACCOUNT_ID": "5",
    "DEFAULT_EMAIL_DOMAIN": "askii.ai",
    "MCP_ALLOWED_CLIENT_REDIRECT_URIS": "https://claude.ai/api/mcp/auth_callback",
}
DISCOVERY = OIDCConfiguration(
    strict=False,
    issuer=ISSUER,
    authorization_endpoint="https://auth.example.test/oauth2/authorize",
    token_endpoint="https://auth.example.test/oauth2/token",
    jwks_uri=f"{ISSUER}/.well-known/jwks.json",
)
ADMIN_ONLY = {"get_report_summary", "get_report_by_group", "get_first_response_distribution", "list_csat_responses"}
# jane is a real address in DEFAULT_EMAIL_DOMAIN, accepted because the overlay stamped it (preferred_username).
USERS = {"ada@example.com": ("sid-ada", "administrator"), "jane@askii.ai": ("sid-jane", "agent")}


@pytest.fixture
def chatwoot() -> FakeSsoChatwoot:
    fake = FakeSsoChatwoot(corporate_gate=True)
    for email, (_username, role) in USERS.items():
        fake.add(email, role=role)
    return fake


@pytest.fixture
def server(monkeypatch, chatwoot):
    monkeypatch.setattr(OIDCProxy, "get_oidc_configuration", lambda *_args, **_kwargs: DISCOVERY)
    mcp = build_platform_server(
        community_settings(ENV),
        PlatformSettings.from_env(ENV),
        store=MemoryKeyValue(),
        oauth_storage=MemoryStore(),
        transport=chatwoot,
    )
    app = mcp.http_app(path="/mcp", stateless_http=True)
    keys = RSAKeyPair.generate()
    mcp.auth._token_validator.public_key = keys.public_key
    mcp.auth._token_validator.jwks_uri = None
    return app, mcp.auth, keys


async def sign_in(provider, keys: RSAKeyPair, email: str, username: str, *, sealed: dict | None = None) -> str:
    """Store what a finished OAuth flow leaves behind, and return the client's FastMCP token."""
    access = keys.create_token(
        subject=f"uuid-{username}",
        issuer=ISSUER,
        scopes=["openid"],
        additional_claims={"token_use": "access", "username": f"uuid-{username}", "client_id": CLIENT_ID},
    )
    id_token = jwt.encode({"email": email, "cognito:username": username, "preferred_username": username}, "k" * 32)
    now = time.time()
    upstream_id, jti = f"up-{username}", f"jti-{username}"
    await provider._upstream_token_store.put(
        key=upstream_id,
        value=UpstreamTokenSet(
            upstream_token_id=upstream_id,
            access_token=access,
            refresh_token=None,
            refresh_token_expires_at=None,
            expires_at=now + 3600,
            token_type="Bearer",
            scope="openid",
            client_id="mcp-client",
            created_at=now,
            raw_token_data={"access_token": access, "id_token": id_token},
        ),
    )
    await provider._jti_mapping_store.put(
        key=jti, value=JTIMapping(jti=jti, upstream_token_id=upstream_id, created_at=now)
    )
    return provider.jwt_issuer.issue_access_token(
        client_id="mcp-client", scopes=["openid"], jti=jti, upstream_claims=sealed
    )


async def rpc(client: httpx.AsyncClient, bearer: str, method: str, params: dict) -> dict:
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = await client.post("/mcp", json=message, headers={**MCP_HEADERS, "Authorization": f"Bearer {bearer}"})
    assert response.status_code == 200, response.text
    data = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    return json.loads(data[-1])["result"]


async def call_get_me(client: httpx.AsyncClient, bearer: str) -> dict:
    result = await rpc(client, bearer, "tools/call", {"name": "get_me", "arguments": {}})
    return result["structuredContent"]["result"]


async def list_tools(client: httpx.AsyncClient, bearer: str) -> set[str]:
    return {tool["name"] for tool in (await rpc(client, bearer, "tools/list", {}))["tools"]}


def owners_of_steady_calls(chatwoot: FakeSsoChatwoot) -> list[str]:
    by_key = {user.key: email for email, user in chatwoot.users.items()}
    return [by_key[r.headers["api_access_token"]] for r in chatwoot.requests if "api_access_token" in r.headers]


async def test_two_concurrent_users_each_get_their_own_identity_and_key(server, chatwoot):
    app, provider, keys = server
    bearers = {email: await sign_in(provider, keys, email, username) for email, (username, _) in USERS.items()}

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
            calls = [call_get_me(client, bearers[email]) for email in USERS for _ in range(3)]
            results = await asyncio.gather(*calls)

    tiers = {"ada@example.com": "admin", "jane@askii.ai": "agent"}
    for email, me in zip([email for email in USERS for _ in range(3)], results, strict=True):
        assert me["tier"] == tiers[email]
    handoffs = [r for r in chatwoot.requests if r.url.path == "/auth/sso/proxy-login"]
    assert sorted(r.headers["x-auth-request-email"] for r in handoffs) == sorted(USERS)
    assert all(r.headers["x-auth-request-access-token"].count(".") == 2 for r in handoffs)
    assert set(owners_of_steady_calls(chatwoot)) == set(USERS)
    assert chatwoot.live_sessions() == []
    for request in chatwoot.requests:
        if "api_access_token" in request.headers:
            assert not any(name.startswith("x-auth-request") for name in request.headers)


async def test_concurrent_tool_lists_are_filtered_per_bearer(server, chatwoot):
    app, provider, keys = server
    bearers = {email: await sign_in(provider, keys, email, username) for email, (username, _) in USERS.items()}

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
            listings = await asyncio.gather(*(list_tools(client, bearers[email]) for email in USERS for _ in range(3)))

    admin_sets, agent_sets = listings[:3], listings[3:]
    assert all(names == admin_sets[0] for names in admin_sets)
    assert all(names == agent_sets[0] for names in agent_sets)
    assert ADMIN_ONLY <= admin_sets[0] and not ADMIN_ONLY & agent_sets[0]
    assert {"get_me", "list_conversations"} <= agent_sets[0] < admin_sets[0]


async def test_the_identity_comes_from_the_stored_token_set_not_the_sealed_copy(server, chatwoot):
    app, provider, keys = server
    forged = jwt.encode({"email": "jane@askii.ai", "cognito:username": "sid-jane"}, "k" * 32)
    bearer = await sign_in(
        provider,
        keys,
        "ada@example.com",
        "sid-ada",
        sealed={"id_token": forged, "email": "jane@askii.ai", "cognito:username": "sid-jane"},
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
            me = await call_get_me(client, bearer)

    assert me["tier"] == "admin"
    handoffs = [r for r in chatwoot.requests if r.url.path == "/auth/sso/proxy-login"]
    assert [r.headers["x-auth-request-email"] for r in handoffs] == ["ada@example.com"]


async def test_a_session_without_a_stored_id_token_is_rejected(server, chatwoot):
    app, provider, keys = server
    bearer = await sign_in(provider, keys, "ada@example.com", "sid-ada")
    stored = await provider._upstream_token_store.get(key="up-sid-ada")
    await provider._upstream_token_store.put(
        key="up-sid-ada", value=stored.model_copy(update={"raw_token_data": {"access_token": stored.access_token}})
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={**MCP_HEADERS, "Authorization": f"Bearer {bearer}"},
            )

    assert response.status_code == 401
    assert chatwoot.requests == []


# --- the upstream token lifetime correction -------------------------------------------------------


def token_response(expires_in, exp_from_now: float | None = 3600) -> dict:
    claims = {} if exp_from_now is None else {"exp": time.time() + exp_from_now}
    return {"access_token": jwt.encode(claims, "k" * 32), "expires_in": expires_in}


def test_the_real_lifetime_comes_from_the_access_tokens_own_exp():
    assert 3590 <= _true_expires_in(token_response(30 * 86400)) <= 3600
    assert _true_expires_in(token_response(5, exp_from_now=-100)) == 60, "an expired token gets the floor"
    assert _true_expires_in(token_response(5, exp_from_now=None)) is None
    assert _true_expires_in({"access_token": "not-a-jwt"}) is None
    assert _true_expires_in({}) is None


def test_the_compliance_hook_rewrites_only_a_wrong_expires_in():
    request = httpx.Request("POST", "https://auth.test/token")
    proxy_says_days = httpx.Response(200, json=token_response(30 * 86400), request=request)
    corrected = _correct_expires_in(proxy_says_days).json()["expires_in"]
    assert 3590 <= corrected <= 3600
    truthful = httpx.Response(200, json=token_response(3600), request=request)
    assert _correct_expires_in(truthful) is truthful
    for untouched in (
        httpx.Response(400, json={"error": "invalid_grant"}, request=request),
        httpx.Response(200, text="not json", request=request),
        httpx.Response(200, json=["a list"], request=request),
        httpx.Response(200, json={"access_token": "opaque"}, request=request),
    ):
        assert _correct_expires_in(untouched) is untouched
