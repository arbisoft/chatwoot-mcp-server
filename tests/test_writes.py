import pytest
from fastmcp.exceptions import ToolError

from tests.conftest import ACCOUNT, call

ON = {"CHATWOOT_MCP_ENABLE_WRITES": "true"}


async def test_add_private_note_posts_a_private_outgoing_message(chatwoot, make_client):
    chatwoot.add(
        "POST",
        f"{ACCOUNT}/conversations/12/messages",
        {"id": 70, "content": "call back Friday", "message_type": 1, "private": True, "created_at": 5},
    )
    out = await call(make_client(**ON), "add_private_note", conversation_id=12, content="call back Friday")
    assert chatwoot.last_json() == {"content": "call back Friday", "message_type": "outgoing", "private": True}
    assert chatwoot.last().headers["api_access_token"] == "user-key"
    assert out["id"] == 70 and out["private"] is True and out["type"] == "outgoing"


async def test_a_note_chatwoot_does_not_mark_private_is_reported_as_a_problem(chatwoot, make_client):
    chatwoot.add(
        "POST", f"{ACCOUNT}/conversations/12/messages", {"id": 70, "content": "x", "message_type": 1, "private": False}
    )
    with pytest.raises(ToolError, match="did not mark the message private"):
        await call(make_client(**ON), "add_private_note", conversation_id=12, content="x")


@pytest.mark.parametrize("content", ["", "   ", "x" * 5001])
async def test_bad_notes_never_reach_chatwoot(chatwoot, make_client, content):
    with pytest.raises(ToolError):
        await call(make_client(**ON), "add_private_note", conversation_id=12, content=content)
    assert chatwoot.requests == []


async def test_a_failed_note_is_a_plain_error(chatwoot, make_client):
    chatwoot.add("POST", f"{ACCOUNT}/conversations/12/messages", {"message": "Conversation is resolved"}, status=422)
    with pytest.raises(ToolError, match="Conversation is resolved"):
        await call(make_client(**ON), "add_private_note", conversation_id=12, content="hi")


def label_routes(chatwoot, existing, known=("vip", "refund", "bug")):
    chatwoot.add("GET", f"{ACCOUNT}/labels", {"payload": [{"id": i, "title": t} for i, t in enumerate(known)]})
    chatwoot.add("GET", f"{ACCOUNT}/conversations/12/labels", {"payload": list(existing)})
    chatwoot.add("POST", f"{ACCOUNT}/conversations/12/labels", {"payload": ["placeholder"]})


async def test_adding_labels_keeps_the_ones_already_there(chatwoot, make_client):
    label_routes(chatwoot, existing=["vip"])
    out = await call(make_client(**ON), "add_conversation_labels", conversation_id=12, labels=["refund"])
    assert chatwoot.last_json() == {"labels": ["vip", "refund"]}
    assert out["conversation_id"] == 12 and out["labels"] == ["placeholder"]


async def test_adding_a_label_twice_does_not_duplicate_it(chatwoot, make_client):
    label_routes(chatwoot, existing=["vip", "refund"])
    await call(make_client(**ON), "add_conversation_labels", conversation_id=12, labels=["refund", "refund", "bug"])
    assert chatwoot.last_json() == {"labels": ["vip", "refund", "bug"]}


async def test_unknown_labels_are_refused_so_none_are_invented(chatwoot, make_client):
    label_routes(chatwoot, existing=[])
    with pytest.raises(ToolError, match="do not exist: made-up"):
        await call(make_client(**ON), "add_conversation_labels", conversation_id=12, labels=["vip", "made-up"])
    assert [r.method for r in chatwoot.requests] == ["GET"]


@pytest.mark.parametrize("labels", [[], ["a"] * 21])
async def test_label_lists_must_be_sensible(chatwoot, make_client, labels):
    with pytest.raises(ToolError):
        await call(make_client(**ON), "add_conversation_labels", conversation_id=12, labels=labels)
    assert chatwoot.requests == []


async def test_writes_are_not_there_unless_switched_on(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(), "add_private_note", conversation_id=12, content="hi")
    assert chatwoot.requests == []
