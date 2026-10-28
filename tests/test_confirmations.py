from chatwoot_mcp.confirmations import Confirmations


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(ttl=300, max_entries=1000):
    clock = Clock()
    return Confirmations(ttl_seconds=ttl, max_entries=max_entries, clock=clock), clock


ARGS = {"conversation_id": 12, "content": "hello"}


def test_a_token_works_once_for_the_same_person_tool_and_arguments():
    store, _ = make()
    token = store.issue("alice", "send_reply", ARGS)
    assert store.redeem("alice", "send_reply", ARGS, token) is True
    assert store.redeem("alice", "send_reply", ARGS, token) is False


def test_tokens_are_long_and_different_each_time():
    store, _ = make()
    first, second = store.issue("a", "t", ARGS), store.issue("a", "t", ARGS)
    assert len(first) >= 20 and first != second


def test_argument_order_does_not_matter():
    store, _ = make()
    token = store.issue("a", "t", {"x": 1, "y": 2})
    assert store.redeem("a", "t", {"y": 2, "x": 1}, token) is True


def test_changed_arguments_are_refused_and_use_up_the_token():
    store, _ = make()
    token = store.issue("alice", "send_reply", ARGS)
    assert store.redeem("alice", "send_reply", {**ARGS, "content": "something else"}, token) is False
    assert store.redeem("alice", "send_reply", ARGS, token) is False


def test_another_person_or_another_tool_cannot_use_the_token():
    store, _ = make()
    assert store.redeem("bob", "send_reply", ARGS, store.issue("alice", "send_reply", ARGS)) is False
    assert store.redeem("alice", "assign_conversation", ARGS, store.issue("alice", "send_reply", ARGS)) is False


def test_an_unknown_or_empty_token_is_refused():
    store, _ = make()
    assert store.redeem("a", "t", ARGS, "nope") is False
    assert store.redeem("a", "t", ARGS, "") is False


def test_a_token_expires():
    store, clock = make(ttl=300)
    token = store.issue("a", "t", ARGS)
    clock.now += 301
    assert store.redeem("a", "t", ARGS, token) is False


def test_a_token_is_still_good_just_before_it_expires():
    store, clock = make(ttl=300)
    token = store.issue("a", "t", ARGS)
    clock.now += 299
    assert store.redeem("a", "t", ARGS, token) is True


def test_the_store_stays_small_and_drops_the_oldest_first():
    store, _ = make(max_entries=3)
    tokens = [store.issue("a", "t", {"n": n}) for n in range(5)]
    assert [store.redeem("a", "t", {"n": n}, tokens[n]) for n in range(5)] == [False, False, True, True, True]
    assert store.ttl_seconds == 300
