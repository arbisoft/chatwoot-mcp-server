import pytest
from fastmcp.server.auth import AuthContext

from chatwoot_mcp.client import ChatwootClient
from chatwoot_mcp.client.errors import InvalidTokenError, MissingTokenError
from chatwoot_mcp.credentials import StaticCredentialProvider, token_identity
from chatwoot_mcp.credentials.base import UNKNOWN_PROFILE, ProfileMemo, fetch_profile
from chatwoot_mcp.credentials.header import HeaderCredentialProvider
from chatwoot_mcp.tiers import (
    Tier,
    community_tier_resolver,
    credential_tier_resolver,
    require_tier,
    required_tier,
    tier_allows,
    tier_for,
)
from tests.conftest import ACCOUNT_ID, ADMIN_PROFILE, BASE, FakeChatwoot, profile


@pytest.mark.parametrize(
    ("permissions", "tier"),
    [
        (None, Tier.NONE),
        (["agent"], Tier.AGENT),
        (["administrator"], Tier.ADMIN),
        (["conversation_manage", "custom_role"], Tier.AGENT),
        (["report_manage", "custom_role"], Tier.ADMIN),
    ],
)
def test_tiers_come_from_the_accounts_permissions(permissions, tier):
    assert tier_for(permissions) is tier


def test_tier_tags_and_comparisons():
    assert required_tier({"tier:agent", "module:x"}) is Tier.AGENT
    assert required_tier({"module:x"}) is None
    assert tier_allows(Tier.ADMIN, Tier.AGENT) and not tier_allows(Tier.AGENT, Tier.ADMIN)
    assert tier_allows(None, Tier.ADMIN), "an unknown caller is not filtered"
    assert Tier.NONE.label == "not a member of this Chatwoot account" and Tier.ADMIN.label == "an administrator"


async def test_the_profile_gives_the_role_in_the_configured_account_only():
    fake = FakeChatwoot(me=profile("administrator", account_id=99))
    client = ChatwootClient(BASE, transport=fake)
    found = await fetch_profile(client, "k", ACCOUNT_ID)
    assert found.tier is Tier.NONE and found.permissions == frozenset()
    fake = FakeChatwoot(me=ADMIN_PROFILE)
    found = await fetch_profile(ChatwootClient(BASE, transport=fake), "k", ACCOUNT_ID)
    assert found.tier is Tier.ADMIN


async def test_the_static_provider_reads_the_role_once():
    fake = FakeChatwoot()
    provider = StaticCredentialProvider(ChatwootClient(BASE, transport=fake), "the-key", ACCOUNT_ID)
    await provider.warm()
    first = await provider.resolve()
    await provider.resolve()
    assert first.tier is Tier.AGENT and first.identity == token_identity("the-key")
    assert "the-key" not in repr(first)
    assert len(fake.requests) == 1


async def test_the_static_provider_needs_a_key():
    provider = StaticCredentialProvider(ChatwootClient(BASE), None, ACCOUNT_ID)
    await provider.warm()
    with pytest.raises(MissingTokenError):
        await provider.resolve()


async def test_a_failed_role_lookup_is_remembered_as_unknown_for_a_minute():
    now = [0.0]
    memo = ProfileMemo(ttl_seconds=60, failure_ttl_seconds=60, clock=lambda: now[0])
    fake = FakeChatwoot()
    fake.on_path("GET", "/api/v1/profile", status=500, json={})
    client = ChatwootClient(BASE, transport=fake, sleep=_no_sleep)
    load = lambda: fetch_profile(client, "k", ACCOUNT_ID)  # noqa: E731
    assert await memo.get("id", load) == UNKNOWN_PROFILE
    calls = len(fake.requests)
    assert await memo.get("id", load) == UNKNOWN_PROFILE
    assert len(fake.requests) == calls
    now[0] = 61
    await memo.get("id", load)
    assert len(fake.requests) > calls


async def _no_sleep(_seconds):
    return None


def test_the_memo_evicts_the_oldest():
    memo = ProfileMemo(ttl_seconds=None, max_entries=1)
    memo._store("a", UNKNOWN_PROFILE)
    memo._store("b", UNKNOWN_PROFILE)
    assert memo.size == 1 and memo._fresh("b") is not None


async def test_the_header_provider_needs_a_header_outside_a_request():
    provider = HeaderCredentialProvider(ChatwootClient(BASE), ACCOUNT_ID)
    with pytest.raises(MissingTokenError):
        await provider.resolve()


async def test_the_header_provider_refuses_a_key_that_cannot_be_a_header(monkeypatch):
    monkeypatch.setattr("chatwoot_mcp.credentials.header.request_token", lambda: "bad key")
    with pytest.raises(InvalidTokenError):
        await HeaderCredentialProvider(ChatwootClient(BASE), ACCOUNT_ID).resolve()


class _Component:
    def __init__(self, tags):
        self.tags = tags


async def test_the_tier_check_hides_tools_above_the_callers_tier():
    async def agent(_token):
        return Tier.AGENT

    check = require_tier(agent)
    assert await check(AuthContext(token=None, component=_Component({"tier:agent"})))
    assert not await check(AuthContext(token=None, component=_Component({"tier:admin"})))
    assert await check(AuthContext(token=None, component=_Component(set())))


async def test_a_failing_resolver_shows_tools_unfiltered_instead_of_blanking_the_list():
    async def broken(_token):
        raise RuntimeError("boom")

    assert await require_tier(broken)(AuthContext(token=None, component=_Component({"tier:admin"})))


async def test_community_mode_filters_only_on_request():
    fake = FakeChatwoot()
    provider = StaticCredentialProvider(ChatwootClient(BASE, transport=fake), "k", ACCOUNT_ID)
    assert await community_tier_resolver(provider, filter_by_role=False)(None) is None
    assert await community_tier_resolver(provider, filter_by_role=True)(None) is Tier.AGENT
    assert await credential_tier_resolver(StaticCredentialProvider(ChatwootClient(BASE), None, 1))(None) is None


async def test_an_unexpected_resolver_error_shows_every_tool():
    class Broken:
        async def resolve(self):
            raise RuntimeError("boom")

    assert await credential_tier_resolver(Broken())(None) is None
