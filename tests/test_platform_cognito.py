from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.aws import AWSCognitoProvider

from chatwoot_mcp.platform.cognito import ACCESS_TOKEN_CLAIM, UPSTREAM_CLAIMS_KEY, ChatwootCognitoProvider
from tests.conftest import id_token_verifier, signed_id_token

ID_TOKEN = signed_id_token(
    {"sub": "uuid-1", "email": "ada@example.com", "cognito:username": "sid-1", "preferred_username": "sid-1"}
)


@pytest.fixture
def provider() -> ChatwootCognitoProvider:
    provider = object.__new__(ChatwootCognitoProvider)
    provider.id_tokens = id_token_verifier()
    return provider


class Store:
    def __init__(self, values: dict) -> None:
        self.values = values

    async def get(self, key):
        return self.values.get(key)


def wire(provider, *, jti="jti-1", mapping=None, token_set=None):
    provider._jwt_issuer = SimpleNamespace(verify_token=lambda _token: {"jti": jti})
    provider._jti_mapping_store = Store({"jti-1": mapping} if mapping is not None else {})
    provider._upstream_token_store = Store({"up-1": token_set} if token_set is not None else {})


async def test_extract_keeps_identity_and_gates_the_access_token(provider):
    tokens = {"id_token": ID_TOKEN, "access_token": "upstream"}

    sealed = await provider._extract_upstream_claims(tokens)
    resolved = await provider._extract_upstream_claims(tokens, include_access_token=True)

    # Nothing is sealed into the JWT a client holds: it is signed, not encrypted.
    assert sealed is None
    assert resolved == {
        "id_token": ID_TOKEN,
        "sub": "uuid-1",
        "email": "ada@example.com",
        "cognito:username": "sid-1",
        "preferred_username": "sid-1",
        ACCESS_TOKEN_CLAIM: "upstream",
    }


@pytest.mark.parametrize("tokens", [{}, {"id_token": ""}, {"id_token": "not-a-jwt"}])
async def test_extract_gives_up_without_a_readable_id_token(provider, tokens):
    assert await provider._extract_upstream_claims(tokens, include_access_token=True) is None


async def test_extract_drops_empty_claims(provider):
    bare = signed_id_token({"email": ""})

    assert await provider._extract_upstream_claims({"id_token": bare}, include_access_token=True) == {"id_token": bare}


async def test_resolve_follows_the_jti_to_the_stored_token_set(provider):
    wire(
        provider,
        mapping=SimpleNamespace(upstream_token_id="up-1"),
        token_set=SimpleNamespace(raw_token_data={"id_token": ID_TOKEN, "access_token": "fresh"}),
    )

    resolved = await provider._resolve_upstream_claims("issued")

    assert resolved["email"] == "ada@example.com"
    assert resolved[ACCESS_TOKEN_CLAIM] == "fresh"


async def test_resolve_accepts_dict_shaped_records(provider):
    wire(provider, mapping={"upstream_token_id": "up-1"}, token_set={"raw_token_data": {"id_token": ID_TOKEN}})

    assert (await provider._resolve_upstream_claims("issued"))["cognito:username"] == "sid-1"


@pytest.mark.parametrize(
    "setup",
    [
        {"jti": None},
        {},
        {"mapping": SimpleNamespace(upstream_token_id=None)},
        {"mapping": SimpleNamespace(upstream_token_id="up-1")},
        {"mapping": SimpleNamespace(upstream_token_id="up-1"), "token_set": SimpleNamespace(raw_token_data=None)},
    ],
    ids=["no-jti", "no-mapping", "no-upstream-id", "no-token-set", "no-raw-data"],
)
async def test_resolve_returns_none_on_any_miss(provider, setup):
    wire(provider, **setup)

    assert await provider._resolve_upstream_claims("issued") is None


async def test_resolve_swallows_verification_errors(provider):
    def reject(_token):
        raise ValueError("bad signature")

    provider._jwt_issuer = SimpleNamespace(verify_token=reject)

    assert await provider._resolve_upstream_claims("issued") is None


async def test_a_store_outage_is_raised_not_reported_as_a_missing_session(provider):
    class DownStore:
        async def get(self, key):
            raise ConnectionError("valkey is down")

    wire(provider)
    provider._jti_mapping_store = DownStore()

    with pytest.raises(ConnectionError):
        await provider._resolve_upstream_claims("issued")


@pytest.fixture
def validated(monkeypatch):
    token = AccessToken(token="t", client_id="c", scopes=["openid"], claims={"sub": "uuid-1"})

    async def upstream_load(self, _token):
        return self.next_validated

    monkeypatch.setattr(AWSCognitoProvider, "load_access_token", upstream_load)
    return token


async def test_load_access_token_attaches_the_current_identity(provider, validated):
    provider.next_validated = validated
    wire(
        provider,
        mapping=SimpleNamespace(upstream_token_id="up-1"),
        token_set=SimpleNamespace(raw_token_data={"id_token": ID_TOKEN, "access_token": "fresh"}),
    )

    loaded = await provider.load_access_token("issued")

    assert loaded.claims["sub"] == "uuid-1"
    assert loaded.claims[UPSTREAM_CLAIMS_KEY]["email"] == "ada@example.com"
    assert loaded.claims[UPSTREAM_CLAIMS_KEY][ACCESS_TOKEN_CLAIM] == "fresh"


async def test_load_access_token_fails_closed_without_an_id_token(provider, validated):
    provider.next_validated = validated
    wire(
        provider,
        mapping=SimpleNamespace(upstream_token_id="up-1"),
        token_set=SimpleNamespace(raw_token_data={"access_token": "only"}),
    )

    assert await provider.load_access_token("issued") is None


async def test_load_access_token_passes_an_upstream_rejection_through(provider, validated):
    provider.next_validated = None
    wire(provider)

    assert await provider.load_access_token("issued") is None


async def test_load_access_token_raises_when_the_store_is_down(provider, validated):
    # FastMCP's base turns a store error into "no token"; the probe makes the outage a server error, not a 401.
    class DownStore:
        async def get(self, key):
            raise ConnectionError("valkey is down")

    provider.next_validated = None
    wire(provider)
    provider._jti_mapping_store = DownStore()

    with pytest.raises(ConnectionError):
        await provider.load_access_token("issued")


@pytest.mark.parametrize(
    "forged",
    [
        jwt.encode({"aud": "chatwoot-mcp-client", "sub": "uuid-1", "email": "boss@example.com"}, "k" * 32),
        signed_id_token({"sub": "uuid-1", "email": "boss@example.com"}, audience="another-client"),
    ],
    ids=["not-signed-by-the-idp", "for-another-client"],
)
async def test_an_id_token_that_does_not_verify_is_rejected(provider, forged):
    # Whoever answers the token endpoint cannot choose the identity the SSO handoff asks Chatwoot for.
    assert await provider._extract_upstream_claims({"id_token": forged}, include_access_token=True) is None


async def test_an_id_token_for_another_user_than_the_access_token_is_rejected(provider, validated):
    provider.next_validated = validated
    other = signed_id_token({"sub": "uuid-2", "email": "boss@example.com"})
    wire(
        provider,
        mapping=SimpleNamespace(upstream_token_id="up-1"),
        token_set=SimpleNamespace(raw_token_data={"id_token": other, "access_token": "fresh"}),
    )
    assert await provider.load_access_token("issued") is None


async def test_the_key_set_is_fetched_once_and_again_only_for_a_new_key():
    from chatwoot_mcp.platform.cognito import JWKS_REFETCH_SECONDS, IdTokenVerifier
    from tests.conftest import ID_TOKEN_AUDIENCE, ID_TOKEN_JWKS

    fetches, now = [], [0.0]

    async def fetch():
        fetches.append(1)
        return ID_TOKEN_JWKS

    verifier = IdTokenVerifier(fetch, ID_TOKEN_AUDIENCE, clock=lambda: now[0])
    assert (await verifier.claims(ID_TOKEN))["sub"] == "uuid-1"
    assert (await verifier.claims(ID_TOKEN))["sub"] == "uuid-1"
    unknown = jwt.encode({"sub": "x"}, "k" * 32, headers={"kid": "rotated"})
    assert await verifier.claims(unknown) is None and len(fetches) == 1
    now[0] += JWKS_REFETCH_SECONDS
    assert await verifier.claims(unknown) is None and len(fetches) == 2
