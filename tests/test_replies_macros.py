import httpx
from fastmcp import Client

from chatwoot_mcp.client.errors import ChatwootTransportError, ServerError
from chatwoot_mcp.server import build_server
from chatwoot_mcp.tools.replies import send_failure
from tests.conftest import FakeChatwoot
from tests.helpers import unframed

CHAT = {"id": 12, "can_reply": True, "meta": {"channel": "Channel::Email", "sender": {"id": 4, "name": "Sam"}}}
REPLY = {"conversation_id": 12, "content": "Hi Sam"}


async def two_steps(settings, fake, prepare, confirm, arguments, *, change=None):
    """Prepare, then confirm in the same server, as an assistant would; returns (preview, result, reuse)."""
    async with Client(build_server(settings, transport=fake)) as client:
        first = (await client.call_tool(prepare, arguments)).data
        if not isinstance(first, dict):
            return first, None, None
        token = first["confirmation_token"]
        done = (await client.call_tool(confirm, {**arguments, **(change or {}), "confirmation_token": token})).data
        again = (await client.call_tool(confirm, {**arguments, "confirmation_token": token})).data
    return first, done, again


def reply_routes(fake: FakeChatwoot, **chat):
    fake.on("GET", "/conversations/12", json={**CHAT, **chat})
    fake.on(
        "POST", "/conversations/12/messages", json={"id": 70, "content": "Hi Sam", "message_type": 1, "private": False}
    )


async def test_a_reply_is_previewed_then_sent_once(everything, fake):
    reply_routes(fake)
    preview, sent, reuse = await two_steps(everything, fake, "prepare_send_reply", "send_reply", REPLY)
    assert unframed(preview["preview"]["to"]) == {"id": 4, "name": "Sam"}
    assert "cannot be unsent" in preview["preview"]["warning"] and "send_reply" in preview["next_step"]
    assert fake.last_json("POST", "/conversations/12/messages") == {
        "content": "Hi Sam",
        "message_type": "outgoing",
        "private": False,
    }
    assert sent["id"] == 70 and sent["url"].endswith("/conversations/12")
    assert "already used" in reuse and len(fake.calls("POST", "/conversations/12/messages")) == 1


async def test_preparing_changes_nothing(everything, fake):
    reply_routes(fake)
    async with Client(build_server(everything, transport=fake)) as client:
        await client.call_tool("prepare_send_reply", REPLY)
    assert fake.writes() == []


async def test_a_changed_message_is_refused(everything, fake):
    reply_routes(fake)
    _, sent, _ = await two_steps(
        everything, fake, "prepare_send_reply", "send_reply", REPLY, change={"content": "Other"}
    )
    assert "does not match" in sent and fake.writes() == []


async def test_a_chat_that_cannot_be_replied_to_gets_no_token(everything, fake):
    reply_routes(fake, can_reply=False)
    preview, _, _ = await two_steps(everything, fake, "prepare_send_reply", "send_reply", REPLY)
    assert "cannot be replied to" in preview


async def test_a_reply_chatwoot_marked_private_is_reported(everything, fake):
    reply_routes(fake)
    fake.on("POST", "/conversations/12/messages", json={"id": 70, "content": "x", "private": True})
    _, sent, _ = await two_steps(everything, fake, "prepare_send_reply", "send_reply", REPLY)
    assert "marked the message private" in sent


def test_a_failed_send_says_whether_it_went():
    request = httpx.Request("POST", "https://chat.test")
    unsent = ChatwootTransportError("x")
    unsent.__cause__ = httpx.ConnectError("refused", request=request)
    maybe = ChatwootTransportError("x")
    maybe.__cause__ = httpx.ReadTimeout("slow", request=request)
    assert "nothing was sent" in send_failure(unsent, "reply")
    assert "may or may not" in send_failure(maybe, "reply")
    assert "may or may not" in send_failure(ServerError(502, "bad gateway"), "reply")
    from chatwoot_mcp.client.errors import ChatwootAPIError

    assert "so it was sent" in send_failure(ChatwootAPIError(200, "response was not JSON"), "reply")


async def test_a_new_conversation_is_previewed_then_created(everything, fake):
    fake.on("GET", "/contacts/4", json={"payload": {"id": 4, "name": "Sam"}})
    fake.on("GET", "/inboxes/8", json={"id": 8, "name": "API", "channel_type": "Channel::Api"})
    fake.on("POST", "/conversations", json={"id": 22, "status": "open", "inbox_id": 8})
    arguments = {"contact_id": 4, "inbox_id": 8, "message": "Hello"}
    preview, created, _ = await two_steps(
        everything, fake, "prepare_create_conversation", "create_conversation", arguments
    )
    assert preview["preview"]["inbox"]["name"] == "API" and "warning" in preview["preview"]
    assert fake.last_json("POST", "/conversations") == {"contact_id": 4, "inbox_id": 8, "message": {"content": "Hello"}}
    assert created["conversation_id"] == 22 and created["url"].endswith("/conversations/22")


async def test_replies_do_not_exist_unless_switched_on_or_when_read_only(settings, read_only, fake):
    for setup in (settings, read_only):
        async with Client(build_server(setup, transport=fake)) as client:
            names = {tool.name for tool in await client.list_tools()}
        assert not names & {"prepare_send_reply", "send_reply", "prepare_run_macro", "run_macro"}


# --- macros ---------------------------------------------------------------------------------------


def macro_routes(fake, actions):
    fake.on("GET", "/conversations/12", json=CHAT)
    fake.on("GET", "/macros/7", json={"payload": {"id": 7, "name": "Refund", "actions": actions}})
    fake.on(
        "GET", "/macros", json={"payload": [{"id": 7, "name": "Refund", "visibility": "global", "actions": actions}]}
    )
    fake.on("POST", "/macros/7/execute", json={})


async def test_a_macro_preview_lists_every_step_and_warns_about_ones_that_reach_outside(everything, fake):
    macro_routes(
        fake,
        [
            {"action_name": "add_label", "action_params": ["refund"]},
            {"action_name": "send_message", "action_params": ["We are on it"]},
            {"action_name": "send_webhook_event", "action_params": ["https://hooks.test"]},
        ],
    )
    arguments = {"macro_id": 7, "conversation_id": 12}
    preview, done, _ = await two_steps(everything, fake, "prepare_run_macro", "run_macro", arguments)
    p = preview["preview"]
    assert p["macro"] == "Refund" and p["steps"][1] == {"step": "send_message", "with": ["We are on it"]}
    assert "sends a message to the customer" in p["warning"] and "outside web address" in p["warning"]
    assert fake.last_json("POST", "/macros/7/execute") == {"conversation_ids": [12]}
    assert done["status"] == "started" and "background" in done["note"]


async def test_a_macro_that_stays_inside_has_no_warning_and_is_listed(everything, fake):
    macro_routes(fake, [{"action_name": "resolve_conversation", "action_params": None}])
    preview, _, _ = await two_steps(
        everything, fake, "prepare_run_macro", "run_macro", {"macro_id": 7, "conversation_id": 12}
    )
    assert "warning" not in preview["preview"]
    async with Client(build_server(everything, transport=fake)) as client:
        listed = (await client.call_tool("list_macros", {})).data
    assert listed == {
        "macros": [{"id": 7, "name": "Refund", "visibility": "global", "steps": [{"step": "resolve_conversation"}]}]
    }


async def test_a_full_confirmation_store_fails_closed(everything, fake, monkeypatch):
    reply_routes(fake)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("valkey down")

    monkeypatch.setattr("chatwoot_mcp.confirmations.Confirmations.issue", broken)
    preview, _, _ = await two_steps(everything, fake, "prepare_send_reply", "send_reply", REPLY)
    assert "confirmation store is unavailable" in preview and fake.writes() == []
