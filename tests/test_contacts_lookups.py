import pytest
from fastmcp.exceptions import ToolError

from tests.conftest import ACCOUNT, call

CONTACT = {
    "id": 4,
    "name": "Sam",
    "email": "sam@example.com",
    "phone_number": "+100",
    "identifier": "u-1",
    "blocked": False,
    "created_at": 1700000000,
    "last_activity_at": 1700000100,
    "thumbnail": "https://img/sam.png",
    "custom_attributes": {"plan": "pro"},
    "contact_inboxes": [{"source_id": "secret-source"}],
}


async def test_search_contacts_returns_short_records(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/contacts/search", {"meta": {"count": 1, "current_page": "1"}, "payload": [CONTACT]})
    out = await call(make_client(), "search_contacts", query="sam")
    assert dict(chatwoot.last().url.params) == {"q": "sam", "page": "1"}
    assert out["meta"]["count"] == 1
    assert out["contacts"][0]["email"] == "sam@example.com"
    assert "thumbnail" not in out["contacts"][0] and "contact_inboxes" not in out["contacts"][0]


async def test_search_contacts_needs_some_words(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(), "search_contacts", query="")
    assert chatwoot.requests == []


async def test_get_contact_unwraps_chatwoots_payload(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/contacts/4", {"payload": CONTACT})
    out = await call(make_client(), "get_contact", contact_id=4)
    assert out["id"] == 4 and out["custom_attributes"] == {"plan": "<untrusted_content>pro</untrusted_content>"}


async def test_get_contact_conversations_lists_their_chats(chatwoot, make_client):
    chat = {"id": 12, "status": "open", "meta": {"channel": "Channel::Email"}}
    chatwoot.add("GET", f"{ACCOUNT}/contacts/4/conversations", {"payload": [chat]})
    out = await call(make_client(), "get_contact_conversations", contact_id=4)
    assert out["contact_id"] == 4 and out["conversations"][0]["id"] == 12


async def test_list_inboxes_keeps_only_useful_fields(chatwoot, make_client):
    inbox = {
        "id": 1,
        "name": "Web",
        "channel_type": "Channel::WebWidget",
        "website_url": "https://x",
        "web_widget_script": "<script>",
        "hmac_token": "secret",
    }
    chatwoot.add("GET", f"{ACCOUNT}/inboxes", {"payload": [inbox]})
    out = await call(make_client(), "list_inboxes")
    assert out["inboxes"] == [
        {"id": 1, "name": "Web", "channel_type": "Channel::WebWidget", "website_url": "https://x", "phone_number": None}
    ]


async def test_list_agents_works_with_a_bare_array_and_hides_extra_fields(chatwoot, make_client):
    agent = {
        "id": 8,
        "name": "Ana",
        "email": "ana@example.com",
        "role": "agent",
        "availability_status": "online",
        "auto_offline": True,
        "custom_role_id": None,
    }
    chatwoot.add("GET", f"{ACCOUNT}/agents", [agent])
    out = await call(make_client(), "list_agents")
    assert out["agents"] == [
        {"id": 8, "name": "Ana", "email": "ana@example.com", "role": "agent", "availability_status": "online"}
    ]


async def test_list_teams_and_labels(chatwoot, make_client):
    chatwoot.add(
        "GET", f"{ACCOUNT}/teams", [{"id": 2, "name": "Support", "description": "d", "allow_auto_assign": True}]
    )
    chatwoot.add(
        "GET",
        f"{ACCOUNT}/labels",
        {"payload": [{"id": 1, "title": "vip", "description": "x", "color": "#fff", "show_on_sidebar": True}]},
    )
    assert (await call(make_client(), "list_teams"))["teams"] == [{"id": 2, "name": "Support", "description": "d"}]
    assert (await call(make_client(), "list_labels"))["labels"] == [{"id": 1, "title": "vip", "description": "x"}]


async def test_list_canned_responses_cuts_long_text(chatwoot, make_client):
    chatwoot.add(
        "GET", f"{ACCOUNT}/canned_responses", [{"id": 1, "short_code": "hi", "content": "y" * 30, "account_id": 5}]
    )
    out = await call(make_client(CHATWOOT_MCP_MAX_TEXT_CHARS="10"), "list_canned_responses")
    assert out["canned_responses"] == [{"id": 1, "short_code": "hi", "content": "y" * 10}]


async def test_lookup_lists_are_capped(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/agents", [{"id": i, "name": f"A{i}"} for i in range(5)])
    out = await call(make_client(CHATWOOT_MCP_MAX_ITEMS="3"), "list_agents")
    assert len(out["agents"]) == 3 and "Only the first 3" in out["truncated"]


async def test_an_unexpected_reply_shape_gives_an_empty_list_not_a_crash(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/teams", {"unexpected": True})
    assert (await call(make_client(), "list_teams"))["teams"] == []


async def test_a_rejected_key_is_reported_in_plain_words(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/teams", {"error": "bad"}, status=401)
    with pytest.raises(ToolError, match="key was rejected"):
        await call(make_client(), "list_teams")


async def test_no_key_means_a_clear_error_and_no_request(chatwoot, make_client):
    with pytest.raises(ToolError, match="No Chatwoot key"):
        await call(make_client(token=None), "list_teams")
    assert chatwoot.requests == []
