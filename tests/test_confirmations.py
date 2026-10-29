import json

import pytest

from chatwoot_mcp.confirmations import (
    KEY_PREFIX,
    ConfirmationError,
    ConfirmationLimitError,
    Confirmations,
    MemoryConfirmationBackend,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def backend(clock) -> MemoryConfirmationBackend:
    return MemoryConfirmationBackend(clock)


@pytest.fixture
def confirmations(backend, clock) -> Confirmations:
    return Confirmations(backend, ttl_seconds=60, clock=clock, max_outstanding_per_identity=3)


PAYLOAD = {"conversation_id": 12, "content": "hi"}


async def test_a_token_works_once_for_the_same_caller_action_and_payload(confirmations):
    token = await confirmations.issue(identity="ada", action="send_reply", payload=PAYLOAD)
    await confirmations.consume(token, identity="ada", action="send_reply", payload=dict(reversed(PAYLOAD.items())))
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.consume(token, identity="ada", action="send_reply", payload=PAYLOAD)


@pytest.mark.parametrize(
    "change",
    [
        {"identity": "eve"},
        {"action": "run_macro"},
        {"payload": {**PAYLOAD, "content": "changed"}},
    ],
)
async def test_a_token_is_spent_and_refused_for_anyone_or_anything_else(confirmations, change):
    token = await confirmations.issue(identity="ada", action="send_reply", payload=PAYLOAD)
    request = {"identity": "ada", "action": "send_reply", "payload": PAYLOAD, **change}
    with pytest.raises(ConfirmationError, match="does not match"):
        await confirmations.consume(token, **request)
    with pytest.raises(ConfirmationError, match="already used"):
        await confirmations.consume(token, identity="ada", action="send_reply", payload=PAYLOAD)


async def test_a_token_expires(confirmations, clock):
    token = await confirmations.issue(identity="ada", action="send_reply", payload=PAYLOAD)
    clock.now += 61
    with pytest.raises(ConfirmationError):
        await confirmations.consume(token, identity="ada", action="send_reply", payload=PAYLOAD)


async def test_unknown_or_empty_tokens_are_refused(confirmations):
    for token in ["", "made-up"]:
        with pytest.raises(ConfirmationError, match="unknown"):
            await confirmations.consume(token, identity="ada", action="send_reply", payload=PAYLOAD)


async def test_the_store_keeps_only_a_hash_of_the_token(confirmations, backend):
    token = await confirmations.issue(identity="ada", action="send_reply", payload=PAYLOAD)
    (key,) = backend._values
    assert key.startswith(KEY_PREFIX) and token not in key
    assert token not in backend._values[key][0] and "hi" not in backend._values[key][0]
    assert json.loads(backend._values[key][0])["identity"] == "ada"


async def test_one_caller_cannot_hold_more_than_their_share(confirmations, clock):
    for _ in range(3):
        await confirmations.issue(identity="ada", action="send_reply", payload=PAYLOAD)
    with pytest.raises(ConfirmationLimitError, match="already have 3"):
        await confirmations.issue(identity="ada", action="send_reply", payload=PAYLOAD)
    await confirmations.issue(identity="bob", action="send_reply", payload=PAYLOAD)
    clock.now += 61
    await confirmations.issue(identity="ada", action="send_reply", payload=PAYLOAD)


async def test_spending_a_token_frees_the_callers_slot(confirmations):
    tokens = [await confirmations.issue(identity="ada", action="a", payload={}) for _ in range(3)]
    await confirmations.consume(tokens[0], identity="ada", action="a", payload={})
    await confirmations.issue(identity="ada", action="a", payload={})


async def test_the_memory_store_has_a_global_cap(clock):
    confirmations = Confirmations(MemoryConfirmationBackend(clock, max_entries=2), clock=clock)
    await confirmations.issue(identity="a", action="x", payload={})
    await confirmations.issue(identity="b", action="x", payload={})
    with pytest.raises(ConfirmationLimitError, match="too many actions"):
        await confirmations.issue(identity="c", action="x", payload={})
    # The refused record did not take one of c's slots.
    assert confirmations._live("c") == {}
