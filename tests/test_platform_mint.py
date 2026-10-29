import logging

import httpx
import pytest

from chatwoot_mcp.platform.identity import Identity
from chatwoot_mcp.platform.mint import (
    RECHECK_INTERVAL_SECONDS,
    HandoffDeniedError,
    KeyedLocks,
    MintedToken,
    MintError,
    NotAMemberError,
    SyntheticIdentityError,
    TokenCache,
    TokenCacheError,
    TokenEndpoint,
    TokenMinter,
)
from chatwoot_mcp.platform.storage import MemoryKeyValue, fernet_for
from chatwoot_mcp.tiers import Tier
from tests.conftest import ACCOUNT_ID, BASE
from tests.platform_fakes import FakeSsoChatwoot

ADA = Identity(email="ada@example.com", access_token="cognito-access")


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def chatwoot():
    fake = FakeSsoChatwoot()
    fake.add("ada@example.com", role="administrator")
    return fake


@pytest.fixture
def store(clock):
    return MemoryKeyValue(clock)


def minter_for(chatwoot, store, clock, **options) -> TokenMinter:
    endpoint = TokenEndpoint(BASE, timeout=httpx.Timeout(5), transport=chatwoot)
    cache = TokenCache(store, fernet_for("s" * 32, "test"), namespace="ns")
    return TokenMinter(endpoint, cache, account_id=ACCOUNT_ID, default_email_domain="askii.ai", clock=clock, **options)


async def test_the_users_own_key_is_fetched_through_the_sso_handoff_and_the_session_is_closed(chatwoot, store, clock):
    minted = await minter_for(chatwoot, store, clock).token_for(ADA)
    assert minted.token == chatwoot.users["ada@example.com"].key
    assert minted.tier is Tier.ADMIN and minted.permissions == frozenset({"administrator"})
    handoff = chatwoot.requests[0]
    assert handoff.headers["x-auth-request-email"] == "ada@example.com"
    assert handoff.headers["x-auth-request-access-token"] == "cognito-access"
    assert chatwoot.live_sessions() == [], "the session sign-in opened must be signed out"
    assert all("cookie" not in r.headers for r in chatwoot.requests)


async def test_the_cache_answers_until_the_recheck_is_due(chatwoot, store, clock):
    minter = minter_for(chatwoot, store, clock)
    first = await minter.token_for(ADA)
    await minter.token_for(ADA)
    assert chatwoot.handoff_count() == 1
    clock.now += RECHECK_INTERVAL_SECONDS
    assert (await minter.token_for(ADA)).token == first.token
    assert chatwoot.handoff_count() == 2


async def test_a_role_change_is_seen_at_the_recheck(chatwoot, store, clock):
    minter = minter_for(chatwoot, store, clock)
    assert (await minter.token_for(ADA)).tier is Tier.ADMIN
    chatwoot.users["ada@example.com"].role = "agent"
    assert await minter.note_forbidden(ADA) is True
    assert await minter.note_forbidden(ADA) is False, "at most one re-check request per five minutes"
    assert (await minter.token_for(ADA)).tier is Tier.AGENT


async def test_a_reset_key_is_fetched_again_once(chatwoot, store, clock):
    minter = minter_for(chatwoot, store, clock)
    old = (await minter.token_for(ADA)).token
    chatwoot.users["ada@example.com"].key = "brand-new-key"
    assert (await minter.renew(ADA, old)).token == "brand-new-key"
    assert (await minter.renew(ADA, old)).token == "brand-new-key"
    assert chatwoot.handoff_count() == 2, "the second renew reuses the key the first one fetched"


@pytest.mark.parametrize("email", ["sid-123@askii.ai", "sid-123"])
async def test_with_email_capture_on_an_unverified_identity_is_refused_before_any_request(
    chatwoot, store, clock, email
):
    identity = Identity(email=email, claims={"cognito:username": "sid-123"})
    with pytest.raises(SyntheticIdentityError, match="verify your email"):
        await minter_for(chatwoot, store, clock, require_verified_email=True).token_for(identity)
    assert chatwoot.requests == [] and chatwoot.created_users == []


@pytest.mark.parametrize("email", ["sid-123@askii.ai", "sid-123"])
async def test_with_email_capture_off_an_unverified_identity_gets_the_web_logins_account(chatwoot, store, clock, email):
    identity = Identity(email=email, claims={"cognito:username": "sid-123"})
    await minter_for(chatwoot, store, clock).token_for(identity)
    assert [r.headers["x-auth-request-email"] for r in chatwoot.requests[:1]] == [email]


async def test_the_corporate_gate_refuses_and_drops_the_cache(store, clock):
    chatwoot = FakeSsoChatwoot(corporate_gate=True)
    chatwoot.add("ada@example.com")
    minter = minter_for(chatwoot, store, clock)
    await minter.token_for(ADA)
    chatwoot.users["ada@example.com"].corporate = False
    clock.now += RECHECK_INTERVAL_SECONDS
    with pytest.raises(HandoffDeniedError, match="corporate-ID gate"):
        await minter.token_for(ADA)
    with pytest.raises(HandoffDeniedError):
        await minter.token_for(ADA)
    assert chatwoot.handoff_count() == 3, "a denied user is never served from the cache"


async def test_sso_mode_off_and_a_handoff_without_a_token_fail_closed(store, clock):
    with pytest.raises(MintError, match="AUTH_TYPE=SSO"):
        await minter_for(FakeSsoChatwoot(sso_mode=False), store, clock).token_for(ADA)
    refusing = FakeSsoChatwoot()
    refusing.handoff_error = "access-denied"
    with pytest.raises(HandoffDeniedError, match="access-denied"):
        await minter_for(refusing, store, clock).token_for(ADA)


async def test_a_user_outside_the_account_is_refused(store, clock):
    chatwoot = FakeSsoChatwoot()
    chatwoot.add("ada@example.com", member=False)
    with pytest.raises(NotAMemberError):
        await minter_for(chatwoot, store, clock).token_for(ADA)


async def test_a_failed_sign_in_still_leaves_no_session_and_no_key(chatwoot, store, clock):
    chatwoot.fail_sign_in = True
    with pytest.raises(Exception):  # noqa: B017
        await minter_for(chatwoot, store, clock).token_for(ADA)
    assert chatwoot.live_sessions() == []
    assert await store.get(TokenCache(store, fernet_for("s" * 32, "test"), namespace="ns").token_key(ADA.key)) is None


async def test_the_cache_is_encrypted_and_never_holds_the_email(chatwoot, store, clock):
    minted = await minter_for(chatwoot, store, clock).token_for(ADA)
    (stored,) = [value for value, _ in store._values.values() if len(value) > 50]
    assert minted.token not in stored and "ada@example.com" not in stored
    assert not any("ada@example.com" in key for key in store._values)


async def test_a_cache_entry_that_no_longer_decrypts_is_fetched_again(chatwoot, store, clock):
    await minter_for(chatwoot, store, clock).token_for(ADA)
    rotated = TokenMinter(
        TokenEndpoint(BASE, timeout=httpx.Timeout(5), transport=chatwoot),
        TokenCache(store, fernet_for("t" * 32, "test"), namespace="ns"),
        account_id=ACCOUNT_ID,
        default_email_domain="askii.ai",
        clock=clock,
    )
    await rotated.token_for(ADA)
    assert chatwoot.handoff_count() == 2


async def test_a_broken_cache_fails_closed(chatwoot, clock):
    class Broken(MemoryKeyValue):
        async def get(self, key):
            raise ConnectionError("valkey down")

    with pytest.raises(TokenCacheError):
        await minter_for(chatwoot, Broken(clock), clock).token_for(ADA)
    assert chatwoot.requests == []


async def test_audit_lines_name_the_event_but_never_the_key_or_email(chatwoot, store, clock, caplog):
    caplog.set_level(logging.INFO, logger="chatwoot_mcp.audit")
    minted = await minter_for(chatwoot, store, clock).token_for(ADA)
    lines = [r.getMessage() for r in caplog.records if r.name == "chatwoot_mcp.audit"]
    assert any('"key.fetched"' in line and '"missing"' in line for line in lines)
    assert not any(minted.token in line or "ada@example.com" in line for line in lines)


def test_minted_tokens_round_trip_and_hide_the_key_in_repr():
    minted = MintedToken(token="k", permissions=frozenset({"agent"}), tier=Tier.AGENT, checked_at=5.0)
    assert MintedToken.from_json(minted.to_json()) == minted and "'k'" not in repr(minted)


async def test_keyed_locks_are_dropped_when_idle():
    locks = KeyedLocks()
    async with locks.hold("a"):
        assert len(locks) == 1
    assert len(locks) == 0


async def test_a_profile_that_is_not_json_is_a_plain_error_not_a_crash(store, clock):
    from chatwoot_mcp.client.errors import UnexpectedResponseError

    chatwoot = FakeSsoChatwoot()
    chatwoot.add("ada@example.com")
    original = chatwoot._profile
    chatwoot._profile = lambda request: httpx.Response(200, text="<html>login</html>")
    with pytest.raises(UnexpectedResponseError, match="not JSON"):
        await minter_for(chatwoot, store, clock).token_for(ADA)
    chatwoot._profile = original
