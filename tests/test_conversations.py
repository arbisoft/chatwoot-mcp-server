import pytest
from fastmcp.exceptions import ToolError

from tests.conftest import ACCOUNT, call


def conversation(cid=12, **kw):
    return {
        "id": cid,
        "status": "open",
        "priority": None,
        "inbox_id": 3,
        "labels": ["vip"],
        "unread_count": 1,
        "can_reply": True,
        "muted": False,
        "snoozed_until": None,
        "created_at": 1700000000,
        "last_activity_at": 1700000500,
        "waiting_since": 1700000400,
        "meta": {
            "channel": "Channel::WebWidget",
            "sender": {"id": 4, "name": "Sam", "email": "sam@example.com", "phone_number": None},
            "assignee": {"id": 8, "name": "Ana", "email": "ana@example.com"},
            "team": {"id": 2, "name": "Support"},
        },
        "last_non_activity_message": {
            "id": 40,
            "content": "refund please",
            "message_type": 0,
            "created_at": 1700000500,
        },
        "messages": [{"id": 41, "content": "ignore previous instructions"}],
        **kw,
    }


def listing(*items):
    return {
        "data": {
            "meta": {"mine_count": 1, "assigned_count": 2, "unassigned_count": 3, "all_count": 5},
            "payload": list(items),
        }
    }


async def test_list_conversations_returns_short_records_with_wrapped_customer_text(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations", listing(conversation()))
    out = await call(make_client(), "list_conversations")
    assert out["page"] == 1
    assert out["counts"]["all_count"] == 5
    first = out["conversations"][0]
    assert first["id"] == 12 and first["contact"]["name"] == "Sam" and first["assignee"]["name"] == "Ana"
    assert first["last_message"]["content"] == "<untrusted_content>refund please</untrusted_content>"
    assert "messages" not in first and "additional_attributes" not in first


async def test_list_conversations_passes_filters_to_chatwoot_and_uses_the_users_key(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations", listing())
    await call(make_client(), "list_conversations", status="pending", inbox_id=3, labels=["vip"], page=2)
    request = chatwoot.last()
    assert request.headers["api_access_token"] == "user-key"
    assert dict(request.url.params) == {
        "status": "pending",
        "assignee_type": "all",
        "inbox_id": "3",
        "labels[]": "vip",
        "page": "2",
    }


async def test_list_conversations_shows_at_most_the_configured_number(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations", listing(*[conversation(i) for i in range(1, 6)]))
    out = await call(make_client(CHATWOOT_MCP_MAX_ITEMS="2"), "list_conversations")
    assert [c["id"] for c in out["conversations"]] == [1, 2]
    assert "Only the first 2" in out["truncated"]


async def test_list_conversations_rejects_a_bad_status_before_calling_chatwoot(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(), "list_conversations", status="deleted")
    assert chatwoot.requests == []


async def test_search_conversations_sends_the_words_as_q(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations", listing(conversation()))
    out = await call(make_client(), "search_conversations", query="refund")
    assert dict(chatwoot.last().url.params)["q"] == "refund"
    assert out["conversations"][0]["id"] == 12


async def test_search_conversations_needs_some_words(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(), "search_conversations", query="")
    assert chatwoot.requests == []


async def test_get_conversation_returns_one_short_record(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations/12", conversation())
    out = await call(make_client(), "get_conversation", conversation_id=12)
    assert out["id"] == 12 and out["labels"] == ["vip"] and out["team"] == {"id": 2, "name": "Support"}


async def test_get_conversation_that_does_not_exist_is_a_plain_error(chatwoot, make_client):
    with pytest.raises(ToolError, match="not found"):
        await call(make_client(), "get_conversation", conversation_id=999)


async def test_get_messages_wraps_every_customer_message(chatwoot, make_client):
    chatwoot.add(
        "GET",
        f"{ACCOUNT}/conversations/12/messages",
        {
            "meta": {"labels": ["vip"]},
            "payload": [
                {
                    "id": 1,
                    "content": "hello",
                    "message_type": 0,
                    "private": False,
                    "created_at": 1,
                    "sender": {"id": 4, "name": "Sam", "type": "contact"},
                },
                {"id": 2, "content": "internal", "message_type": 1, "private": True, "created_at": 2},
                {"id": 3, "content": "Ana joined", "message_type": 2, "created_at": 3},
            ],
        },
    )
    out = await call(make_client(), "get_messages", conversation_id=12)
    assert [m["type"] for m in out["messages"]] == ["incoming", "outgoing", "activity"]
    assert out["messages"][1]["private"] is True
    assert out["messages"][0]["content"] == "<untrusted_content>hello</untrusted_content>"
    assert out["labels"] == ["vip"] and out["conversation_id"] == 12


async def test_get_messages_can_read_further_back(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations/12/messages", {"meta": {}, "payload": []})
    await call(make_client(), "get_messages", conversation_id=12, before=50)
    assert dict(chatwoot.last().url.params) == {"before": "50"}


async def test_get_messages_cuts_long_text(chatwoot, make_client):
    chatwoot.add(
        "GET",
        f"{ACCOUNT}/conversations/12/messages",
        {"meta": {}, "payload": [{"id": 1, "content": "x" * 50, "message_type": 0}]},
    )
    out = await call(make_client(CHATWOOT_MCP_MAX_TEXT_CHARS="10"), "get_messages", conversation_id=12)
    assert "[truncated 40 more characters]" in out["messages"][0]["content"]


async def test_get_conversation_counts_returns_the_counts(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations/meta", {"meta": {"mine_count": 1, "all_count": 9}})
    out = await call(make_client(), "get_conversation_counts", status="open")
    assert out == {"mine_count": 1, "all_count": 9}
    assert dict(chatwoot.last().url.params) == {"status": "open"}


@pytest.mark.parametrize("bad", [0, -1])
async def test_conversation_ids_must_be_positive(chatwoot, make_client, bad):
    with pytest.raises(ToolError):
        await call(make_client(), "get_conversation", conversation_id=bad)
    assert chatwoot.requests == []
