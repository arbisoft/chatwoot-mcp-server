import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from chatwoot_mcp.server import build_server
from tests.conftest import ACCOUNT, BASE, call, tool_names

ON = {"CHATWOOT_MCP_ENABLE_ACTIONS": "true"}
CONV = {
    "id": 12,
    "status": "open",
    "can_reply": True,
    "inbox_id": 3,
    "meta": {
        "channel": "Channel::WebWidget",
        "sender": {"id": 4, "name": "Sam", "email": "sam@example.com", "phone_number": None},
        "assignee": {"id": 9, "name": "Old Agent", "email": "old@example.com"},
        "team": None,
    },
}
AGENTS = [{"id": 8, "name": "Ana", "email": "ana@example.com", "role": "agent"}]
TEAMS = [{"id": 2, "name": "Support", "description": "d"}]
ACTION_TOOLS = {
    "send_reply",
    "assign_conversation",
    "set_conversation_status",
    "set_conversation_priority",
    "create_conversation",
    "create_contact",
    "update_contact",
}


def changes(chatwoot):
    return [r for r in chatwoot.requests if r.method != "GET"]


async def preview(client, tool, /, **arguments):
    out = await call(client, tool, **arguments)
    assert out["status"] == "needs_confirmation" and out["confirm_token"]
    return out


def conversation_routes(chatwoot, **conv):
    chatwoot.add("GET", f"{ACCOUNT}/conversations/12", {**CONV, **conv})


async def test_action_tools_exist_only_when_switched_on(make_client):
    assert not ACTION_TOOLS & await tool_names(make_client())
    assert not ACTION_TOOLS & await tool_names(make_client(CHATWOOT_MCP_ENABLE_WRITES="true"))
    assert ACTION_TOOLS <= await tool_names(make_client(**ON))


async def test_action_tools_are_tagged_and_hinted_honestly(make_client):
    async with make_client(**ON) as client:
        tools = {t.name: t for t in await client.list_tools() if t.name in ACTION_TOOLS}
    for tool in tools.values():
        assert "tier:action" in tool.meta["fastmcp"]["tags"]
        assert tool.annotations.readOnlyHint is False
    additive = {"send_reply", "create_conversation", "create_contact"}
    for name, tool in tools.items():
        assert tool.annotations.destructiveHint is (name not in additive), name


# --- send_reply -----------------------------------------------------------------------------------


async def test_send_reply_first_call_changes_nothing_and_shows_who_gets_the_message(chatwoot, make_client):
    conversation_routes(chatwoot)
    out = await preview(make_client(**ON), "send_reply", conversation_id=12, content="Your refund is on its way.")
    assert changes(chatwoot) == []
    p = out["preview"]
    assert p["to"] == {"name": "Sam", "email": "sam@example.com"}
    assert p["channel"] == "Channel::WebWidget" and p["message"] == "Your refund is on its way."
    assert "sent to the customer" in p["warning"] and "cannot be unsent" in p["warning"]
    assert "Show this preview to the user" in out["next_step"]


async def test_send_reply_second_call_with_the_token_sends_it(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add(
        "POST",
        f"{ACCOUNT}/conversations/12/messages",
        {"id": 90, "content": "ok", "message_type": 1, "private": False, "created_at": 7},
    )
    client = make_client(**ON)
    token = (await preview(client, "send_reply", conversation_id=12, content="ok"))["confirm_token"]
    out = await call(client, "send_reply", conversation_id=12, content="ok", confirm_token=token)
    assert len(changes(chatwoot)) == 1
    assert chatwoot.last_json() == {"content": "ok", "message_type": "outgoing", "private": False}
    assert out["id"] == 90 and out["type"] == "outgoing" and out["private"] is False


async def test_a_token_cannot_be_used_twice(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add("POST", f"{ACCOUNT}/conversations/12/messages", {"id": 90, "message_type": 1, "private": False})
    client = make_client(**ON)
    token = (await preview(client, "send_reply", conversation_id=12, content="ok"))["confirm_token"]
    await call(client, "send_reply", conversation_id=12, content="ok", confirm_token=token)
    with pytest.raises(ToolError, match="confirmation"):
        await call(client, "send_reply", conversation_id=12, content="ok", confirm_token=token)
    assert len(changes(chatwoot)) == 1


async def test_changing_the_message_after_the_preview_is_refused(chatwoot, make_client):
    conversation_routes(chatwoot)
    client = make_client(**ON)
    token = (await preview(client, "send_reply", conversation_id=12, content="ok"))["confirm_token"]
    with pytest.raises(ToolError, match="confirmation"):
        await call(client, "send_reply", conversation_id=12, content="something different", confirm_token=token)
    assert changes(chatwoot) == []


async def test_a_made_up_token_is_refused(chatwoot, make_client):
    conversation_routes(chatwoot)
    with pytest.raises(ToolError, match="confirmation"):
        await call(make_client(**ON), "send_reply", conversation_id=12, content="ok", confirm_token="invented")
    assert changes(chatwoot) == []


async def test_another_persons_token_does_not_work(chatwoot, make_settings):
    conversation_routes(chatwoot)
    chatwoot.add("POST", f"{ACCOUNT}/conversations/12/messages", {"id": 90, "message_type": 1, "private": False})
    holder = {"key": "alice-key"}
    server = build_server(
        make_settings(**ON), token_getter=lambda: holder["key"], transport=httpx.MockTransport(chatwoot)
    )
    async with Client(server) as client:
        token = (await client.call_tool("send_reply", {"conversation_id": 12, "content": "ok"})).data["confirm_token"]
        holder["key"] = "bob-key"
        with pytest.raises(ToolError, match="confirmation"):
            await client.call_tool("send_reply", {"conversation_id": 12, "content": "ok", "confirm_token": token})
    assert changes(chatwoot) == []


async def test_a_chat_that_cannot_be_replied_to_gets_no_token(chatwoot, make_client):
    conversation_routes(chatwoot, can_reply=False)
    with pytest.raises(ToolError, match="cannot be replied to"):
        await call(make_client(**ON), "send_reply", conversation_id=12, content="ok")
    assert changes(chatwoot) == []


@pytest.mark.parametrize("content", ["", "   ", "x" * 5001])
async def test_bad_replies_never_reach_chatwoot(chatwoot, make_client, content):
    with pytest.raises(ToolError):
        await call(make_client(**ON), "send_reply", conversation_id=12, content=content)
    assert chatwoot.requests == []


async def test_a_reply_chatwoot_marks_private_is_reported_not_hidden(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add("POST", f"{ACCOUNT}/conversations/12/messages", {"id": 90, "message_type": 1, "private": True})
    client = make_client(**ON)
    token = (await preview(client, "send_reply", conversation_id=12, content="ok"))["confirm_token"]
    with pytest.raises(ToolError, match="marked the message private"):
        await call(client, "send_reply", conversation_id=12, content="ok", confirm_token=token)


# --- assign_conversation --------------------------------------------------------------------------


async def test_assign_needs_an_agent_or_a_team(chatwoot, make_client):
    with pytest.raises(ToolError, match="agent_id or team_id"):
        await call(make_client(**ON), "assign_conversation", conversation_id=12)
    assert chatwoot.requests == []


async def test_assign_preview_names_who_and_who_it_replaces(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add("GET", f"{ACCOUNT}/agents", AGENTS)
    chatwoot.add("GET", f"{ACCOUNT}/teams", TEAMS)
    out = await preview(make_client(**ON), "assign_conversation", conversation_id=12, agent_id=8, team_id=2)
    p = out["preview"]
    assert p["assign_to_agent"]["name"] == "Ana" and p["assign_to_team"]["name"] == "Support"
    assert p["currently"]["agent"]["name"] == "Old Agent"
    assert changes(chatwoot) == []
    assert "side_effect" not in p


async def test_assigning_only_a_team_warns_the_current_agent_may_be_removed(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add("GET", f"{ACCOUNT}/teams", TEAMS)
    out = await preview(make_client(**ON), "assign_conversation", conversation_id=12, team_id=2)
    assert "not in that team, Chatwoot removes them" in out["preview"]["side_effect"]


async def test_assign_to_an_agent_or_team_that_does_not_exist_is_refused_before_any_token(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add("GET", f"{ACCOUNT}/agents", AGENTS)
    with pytest.raises(ToolError, match="No agent with id 99"):
        await call(make_client(**ON), "assign_conversation", conversation_id=12, agent_id=99)
    chatwoot.add("GET", f"{ACCOUNT}/teams", TEAMS)
    with pytest.raises(ToolError, match="No team with id 99"):
        await call(make_client(**ON), "assign_conversation", conversation_id=12, team_id=99)


async def test_assign_sends_the_team_then_the_agent_so_the_team_change_cannot_drop_the_agent(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add("GET", f"{ACCOUNT}/agents", AGENTS)
    chatwoot.add("GET", f"{ACCOUNT}/teams", TEAMS)
    chatwoot.add("POST", f"{ACCOUNT}/conversations/12/assignments", {})
    client = make_client(**ON)
    token = (await preview(client, "assign_conversation", conversation_id=12, agent_id=8, team_id=2))["confirm_token"]
    out = await call(client, "assign_conversation", conversation_id=12, agent_id=8, team_id=2, confirm_token=token)
    import json

    assert [json.loads(r.read()) for r in changes(chatwoot)] == [{"team_id": 2}, {"assignee_id": 8}]
    assert out == {"conversation_id": 12, "assigned_agent_id": 8, "assigned_team_id": 2}


async def test_assign_says_clearly_when_only_part_of_it_worked(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add("GET", f"{ACCOUNT}/agents", AGENTS)
    chatwoot.add("GET", f"{ACCOUNT}/teams", TEAMS)
    calls = []

    def handler(request):
        if request.method == "POST":
            calls.append(request)
            return httpx.Response(200 if len(calls) == 1 else 422, json={"message": "Agent is not an inbox member"})
        return chatwoot(request)

    server = build_server(chatwoot_settings(), token_getter=lambda: "user-key", transport=httpx.MockTransport(handler))
    async with Client(server) as client:
        token = (
            await client.call_tool("assign_conversation", {"conversation_id": 12, "agent_id": 8, "team_id": 2})
        ).data["confirm_token"]
        with pytest.raises(ToolError, match="team was assigned, but assigning the agent failed"):
            await client.call_tool(
                "assign_conversation", {"conversation_id": 12, "agent_id": 8, "team_id": 2, "confirm_token": token}
            )


def chatwoot_settings():
    from chatwoot_mcp.config import Settings

    return Settings.from_env({"CHATWOOT_BASE_URL": BASE, "CHATWOOT_ACCOUNT_ID": "5", **ON})


# --- set_conversation_status ----------------------------------------------------------------------


async def test_reopening_warns_that_chatwoot_assigns_the_chat_to_the_acting_agent(chatwoot, make_client):
    conversation_routes(chatwoot, status="resolved")
    out = await preview(make_client(**ON), "set_conversation_status", conversation_id=12, status="open")
    assert out["preview"]["from"] == "resolved" and out["preview"]["to"] == "open"
    assert "has the agent role (not administrator), Chatwoot also assigns" in out["preview"]["side_effect"]


async def test_resolving_has_no_side_effect_warning(chatwoot, make_client):
    conversation_routes(chatwoot)
    out = await preview(make_client(**ON), "set_conversation_status", conversation_id=12, status="resolved")
    assert "side_effect" not in out["preview"]


async def test_status_change_is_sent_after_confirmation(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add(
        "POST",
        f"{ACCOUNT}/conversations/12/toggle_status",
        {"payload": {"success": True, "conversation_id": 12, "current_status": "resolved", "snoozed_until": None}},
    )
    client = make_client(**ON)
    token = (await preview(client, "set_conversation_status", conversation_id=12, status="resolved"))["confirm_token"]
    out = await call(client, "set_conversation_status", conversation_id=12, status="resolved", confirm_token=token)
    assert chatwoot.last_json() == {"status": "resolved"}
    assert out == {"conversation_id": 12, "status": "resolved", "snoozed_until": None}


async def test_snoozing_needs_a_future_time_with_a_time_zone(chatwoot, make_client):
    conversation_routes(chatwoot)
    chatwoot.add(
        "POST",
        f"{ACCOUNT}/conversations/12/toggle_status",
        {"payload": {"current_status": "snoozed", "snoozed_until": "2099-01-01T09:00:00Z"}},
    )
    client = make_client(**ON)
    for bad in [None, "2099-01-01T09:00:00", "2020-01-01T00:00:00+00:00", "tomorrow"]:
        with pytest.raises(ToolError):
            await call(client, "set_conversation_status", conversation_id=12, status="snoozed", snoozed_until=bad)
    token = (
        await preview(
            client,
            "set_conversation_status",
            conversation_id=12,
            status="snoozed",
            snoozed_until="2099-01-01T09:00:00+00:00",
        )
    )["confirm_token"]
    await call(
        client,
        "set_conversation_status",
        conversation_id=12,
        status="snoozed",
        snoozed_until="2099-01-01T09:00:00+00:00",
        confirm_token=token,
    )
    # Chatwoot reads snoozed_until with strptime('%s'): an ISO string would become a time in 1970.
    assert chatwoot.last_json() == {"status": "snoozed", "snoozed_until": 4070941200}


async def test_a_snooze_time_with_any_other_status_is_refused(chatwoot, make_client):
    with pytest.raises(ToolError, match="only with status snoozed"):
        await call(
            make_client(**ON),
            "set_conversation_status",
            conversation_id=12,
            status="open",
            snoozed_until="2099-01-01T09:00:00+00:00",
        )
    assert chatwoot.requests == []


async def test_a_bad_status_is_refused(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(**ON), "set_conversation_status", conversation_id=12, status="deleted")
    assert chatwoot.requests == []


# --- set_conversation_priority --------------------------------------------------------------------


@pytest.mark.parametrize(("given", "sent"), [("urgent", "urgent"), ("low", "low"), ("none", None)])
async def test_priority_is_sent_after_confirmation_and_none_clears_it(chatwoot, make_client, given, sent):
    conversation_routes(chatwoot, priority="high")
    chatwoot.add("POST", f"{ACCOUNT}/conversations/12/toggle_priority", {})
    client = make_client(**ON)
    out = await preview(client, "set_conversation_priority", conversation_id=12, priority=given)
    assert out["preview"]["from"] == "high" and out["preview"]["to"] == given
    result = await call(
        client, "set_conversation_priority", conversation_id=12, priority=given, confirm_token=out["confirm_token"]
    )
    assert chatwoot.last_json() == {"priority": sent}
    assert result == {"conversation_id": 12, "priority": given}


async def test_a_bad_priority_is_refused(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(**ON), "set_conversation_priority", conversation_id=12, priority="critical")
    assert chatwoot.requests == []


# --- create_conversation --------------------------------------------------------------------------


def create_routes(chatwoot):
    chatwoot.add("GET", f"{ACCOUNT}/contacts/4", {"payload": {"id": 4, "name": "Sam", "email": "sam@example.com"}})
    chatwoot.add("GET", f"{ACCOUNT}/inboxes/3", {"id": 3, "name": "Support email", "channel_type": "Channel::Email"})
    chatwoot.add("POST", f"{ACCOUNT}/conversations", {"id": 77, "status": "open", "inbox_id": 3})


async def test_create_conversation_with_a_message_warns_it_goes_to_the_customer(chatwoot, make_client):
    create_routes(chatwoot)
    client = make_client(**ON)
    out = await preview(client, "create_conversation", contact_id=4, inbox_id=3, message="Hello Sam")
    p = out["preview"]
    assert p["contact"]["name"] == "Sam" and p["inbox"]["name"] == "Support email" and p["first_message"] == "Hello Sam"
    assert "sent to the customer" in p["warning"]
    result = await call(
        client, "create_conversation", contact_id=4, inbox_id=3, message="Hello Sam", confirm_token=out["confirm_token"]
    )
    assert chatwoot.last_json() == {"contact_id": 4, "inbox_id": 3, "message": {"content": "Hello Sam"}}
    assert result == {"conversation_id": 77, "status": "open", "inbox_id": 3}


async def test_create_conversation_without_a_message_sends_nothing_to_the_customer(chatwoot, make_client):
    create_routes(chatwoot)
    client = make_client(**ON)
    out = await preview(client, "create_conversation", contact_id=4, inbox_id=3)
    assert out["preview"]["first_message"] is None and "warning" not in out["preview"]
    await call(client, "create_conversation", contact_id=4, inbox_id=3, confirm_token=out["confirm_token"])
    assert chatwoot.last_json() == {"contact_id": 4, "inbox_id": 3}


# --- create_contact / update_contact --------------------------------------------------------------


async def test_create_contact_needs_a_way_to_reach_them(chatwoot, make_client):
    with pytest.raises(ToolError, match="email, phone_number or identifier"):
        await call(make_client(**ON), "create_contact", name="Sam")
    assert chatwoot.requests == []


@pytest.mark.parametrize("phone", ["0123", "+12", "12345678901", "+1 555 0100", "+0123456789"])
async def test_a_phone_number_must_be_in_international_format(chatwoot, make_client, phone):
    with pytest.raises(ToolError, match="international format"):
        await call(make_client(**ON), "create_contact", name="Sam", phone_number=phone)
    assert chatwoot.requests == []


async def test_create_contact_is_sent_after_confirmation(chatwoot, make_client):
    chatwoot.add(
        "POST",
        f"{ACCOUNT}/contacts",
        {
            "payload": {
                "contact": {"id": 4, "name": "Sam", "email": "sam@example.com"},
                "contact_inbox": {"inbox": {"hmac_token": "SECRET"}},
            }
        },
    )
    client = make_client(**ON)
    out = await preview(client, "create_contact", name="Sam", email="sam@example.com", phone_number="+15550100")
    assert out["preview"]["new_contact"] == {"name": "Sam", "email": "sam@example.com", "phone_number": "+15550100"}
    assert changes(chatwoot) == []
    result = await call(
        client,
        "create_contact",
        name="Sam",
        email="sam@example.com",
        phone_number="+15550100",
        confirm_token=out["confirm_token"],
    )
    assert chatwoot.last_json() == {"name": "Sam", "email": "sam@example.com", "phone_number": "+15550100"}
    assert result["id"] == 4 and "SECRET" not in str(result)


async def test_a_duplicate_email_is_a_plain_error_with_chatwoots_reason(chatwoot, make_client):
    chatwoot.add("POST", f"{ACCOUNT}/contacts", {"message": "Email has already been taken"}, status=422)
    client = make_client(**ON)
    token = (await preview(client, "create_contact", name="Sam", email="sam@example.com"))["confirm_token"]
    with pytest.raises(ToolError, match="Email has already been taken"):
        await call(client, "create_contact", name="Sam", email="sam@example.com", confirm_token=token)


async def test_update_contact_needs_something_to_change(chatwoot, make_client):
    with pytest.raises(ToolError, match="at least one"):
        await call(make_client(**ON), "update_contact", contact_id=4)
    assert chatwoot.requests == []


async def test_update_contact_shows_before_and_after_and_sends_only_what_changed(chatwoot, make_client):
    chatwoot.add(
        "GET",
        f"{ACCOUNT}/contacts/4",
        {"payload": {"id": 4, "name": "Sam", "email": "old@example.com", "phone_number": None}},
    )
    chatwoot.add("PUT", f"{ACCOUNT}/contacts/4", {"payload": {"id": 4, "name": "Sam", "email": "new@example.com"}})
    client = make_client(**ON)
    out = await preview(client, "update_contact", contact_id=4, email="new@example.com")
    assert out["preview"]["changes"] == {"email": {"from": "old@example.com", "to": "new@example.com"}}
    result = await call(
        client, "update_contact", contact_id=4, email="new@example.com", confirm_token=out["confirm_token"]
    )
    assert chatwoot.last().method == "PUT" and chatwoot.last_json() == {"email": "new@example.com"}
    assert result["email"] == "new@example.com"


async def test_updating_to_the_values_a_contact_already_has_is_refused(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/contacts/4", {"payload": {"id": 4, "name": "Sam", "email": "same@example.com"}})
    with pytest.raises(ToolError, match="already has"):
        await call(make_client(**ON), "update_contact", contact_id=4, email="same@example.com")


async def test_actions_without_a_key_stop_with_a_plain_error(chatwoot, make_client):
    with pytest.raises(ToolError, match="No Chatwoot key"):
        await call(make_client(token=None, **ON), "create_contact", name="Sam", email="sam@example.com")
    assert chatwoot.requests == []
