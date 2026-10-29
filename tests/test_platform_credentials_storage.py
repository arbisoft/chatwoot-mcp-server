import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastmcp import Client
from fastmcp.server.auth import AccessToken

from chatwoot_mcp.client import ChatwootClient
from chatwoot_mcp.client.errors import AuthError, GatewayDeniedError, MissingTokenError, PermissionDenied
from chatwoot_mcp.confirmations import ConfirmationError, Confirmations
from chatwoot_mcp.platform.credentials import MintedCredentialProvider, is_role_denial, platform_tier_resolver
from chatwoot_mcp.platform.identity import Identity
from chatwoot_mcp.platform.mint import TokenCache, TokenEndpoint, TokenMinter
from chatwoot_mcp.platform.storage import (
    MemoryKeyValue,
    RedisKeyValue,
    ValkeyConfirmationBackend,
    fernet_for,
    parse_storage_url,
)
from chatwoot_mcp.server import build_server
from chatwoot_mcp.tiers import Tier
from tests.conftest import ACCOUNT_ID, BASE
from tests.platform_fakes import FakeSsoChatwoot

ADA = Identity(email="ada@example.com")


def minter(chatwoot, store=None) -> TokenMinter:
    return TokenMinter(
        TokenEndpoint(BASE, timeout=httpx.Timeout(5), transport=chatwoot),
        TokenCache(store or MemoryKeyValue(), fernet_for("s" * 32, "test")),
        account_id=ACCOUNT_ID,
        default_email_domain="askii.ai",
    )


@pytest.fixture
def chatwoot():
    fake = FakeSsoChatwoot()
    fake.add("ada@example.com")
    return fake


async def test_the_provider_gives_the_signed_in_users_key_and_a_stable_identity(chatwoot):
    provider = MintedCredentialProvider(minter(chatwoot), identity_source=lambda: ADA)
    credential = await provider.resolve()
    assert credential.token == chatwoot.users["ada@example.com"].key
    assert credential.identity == ADA.short_key and credential.tier is Tier.AGENT


async def test_no_signed_in_identity_fails_closed(chatwoot):
    provider = MintedCredentialProvider(minter(chatwoot), identity_source=lambda: None)
    with pytest.raises(MissingTokenError, match="sign in again"):
        await provider.resolve()
    assert await provider.on_rejected(AuthError(401, "x"), "k") is None


async def test_a_401_fetches_the_key_again_and_a_role_refusal_schedules_a_recheck(chatwoot):
    keys = minter(chatwoot)
    provider = MintedCredentialProvider(keys, identity_source=lambda: ADA)
    old = (await provider.resolve()).token
    chatwoot.users["ada@example.com"].key = "new-key"
    assert await provider.on_rejected(AuthError(401, "Invalid Access Token"), old) == "new-key"
    assert (
        await provider.on_rejected(PermissionDenied(401, "You are not authorized to do this action"), "new-key") is None
    )
    assert (await keys._cache.load(ADA.key)).recheck_requested


def test_only_chatwoots_own_refusals_count_as_role_denials():
    assert is_role_denial(PermissionDenied(403, "x")) and not is_role_denial(GatewayDeniedError(403, "access_denied"))


async def test_the_platform_tier_resolver_fails_closed(chatwoot):
    provider = MintedCredentialProvider(minter(chatwoot), identity_source=lambda: ADA)
    resolve = platform_tier_resolver(provider, identity_of=lambda token: ADA if token else None)
    assert await resolve(None) is Tier.NONE
    assert await resolve(AccessToken(token="t", client_id="c", scopes=[])) is Tier.AGENT

    def broken(_token):
        raise RuntimeError("boom")

    assert await platform_tier_resolver(provider, identity_of=broken)(None) is Tier.NONE


async def test_a_signed_in_users_tool_calls_run_with_their_own_key_end_to_end(settings, chatwoot):
    import dataclasses

    calls = []

    def api(request):
        calls.append(request.headers.get("api_access_token"))
        return chatwoot.handler(request)

    transport = httpx.MockTransport(api)
    provider = MintedCredentialProvider(minter(transport), identity_source=lambda: ADA)
    server = build_server(
        dataclasses.replace(settings, http_token=None),
        client=ChatwootClient(BASE, transport=transport, on_rejected=provider.on_rejected),
        credentials=provider,
    )
    async with Client(server) as client:
        me = (await client.call_tool("get_me", {})).data
    assert me["tier"] == "agent"
    assert chatwoot.users["ada@example.com"].key in calls and settings.http_token not in calls


# --- storage --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "url", "db"),
    [
        ("valkey://cache:6379/2", "redis://cache:6379/2", 2),
        ("valkeys://cache/0?ssl_cert_reqs=none", "rediss://cache/0?ssl_cert_reqs=none", 0),
        ("redis://cache", "redis://cache", 0),
    ],
)
def test_storage_urls_map_valkey_and_keep_the_query(raw, url, db):
    config = parse_storage_url(raw)
    assert (config.url, config.db) == (url, db)


@pytest.mark.parametrize(
    ("raw", "message"), [("http://x", "scheme"), ("redis://", "hostname"), ("redis://x/a", "numeric")]
)
def test_bad_storage_urls_are_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_storage_url(raw)


async def test_the_memory_store_expires_and_takes_once():
    now = [0.0]
    store = MemoryKeyValue(lambda: now[0])
    await store.set("a", "1", 10)
    assert await store.set_if_absent("a", "2", 10) is False
    assert await store.take("a") == "1" and await store.take("a") is None
    await store.set("b", "1", 10)
    now[0] = 11
    assert await store.get("b") is None and await store.set_if_absent("b", "2", 10) is True
    await store.delete("b")
    assert await store.get("b") is None


async def test_valkey_keeps_confirmations_encrypted_and_shared_between_replicas():
    redis = FakeAsyncRedis(decode_responses=True)
    fernet = fernet_for("s" * 32, "confirm")
    one = Confirmations(ValkeyConfirmationBackend(redis, fernet, namespace="ns"))
    two = Confirmations(ValkeyConfirmationBackend(redis, fernet, namespace="ns"))
    token = await one.issue(identity="ada", action="send_reply", payload={"c": 1})
    (key,) = await redis.keys("*")
    assert "ada" not in await redis.get(key) and token not in key
    await two.consume(token, identity="ada", action="send_reply", payload={"c": 1})
    with pytest.raises(ConfirmationError):
        await one.consume(token, identity="ada", action="send_reply", payload={"c": 1})


async def test_a_confirmation_sealed_with_another_key_reads_as_absent():
    redis = FakeAsyncRedis(decode_responses=True)
    old = ValkeyConfirmationBackend(redis, fernet_for("a" * 32, "x"), namespace="ns")
    new = ValkeyConfirmationBackend(redis, fernet_for("b" * 32, "x"), namespace="ns")
    await old.put("k", "v", 60)
    assert await new.take("k") is None and await old.take("k") is None


async def test_the_redis_store_speaks_the_key_value_protocol():
    store = RedisKeyValue(FakeAsyncRedis(decode_responses=True))
    await store.set("a", "1", 10)
    assert await store.get("a") == "1" and await store.set_if_absent("a", "2", 10) is False
    assert await store.take("a") == "1"
    await store.delete("a")
