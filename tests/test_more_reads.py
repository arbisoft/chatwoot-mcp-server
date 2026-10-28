import pytest
from fastmcp.exceptions import ToolError

from tests.conftest import ACCOUNT, call

V2 = "/api/v2/accounts/5"
CONTACT = {"id": 4, "name": "Sam", "email": "sam@example.com", "thumbnail": "x", "custom_attributes": {}}


async def test_list_contacts_pages_and_filters_by_label(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/contacts", {"meta": {"count": 31, "current_page": "2"}, "payload": [CONTACT]})
    out = await call(make_client(), "list_contacts", page=2, labels=["vip"])
    assert dict(chatwoot.last().url.params) == {"page": "2", "labels[]": "vip"}
    assert out["meta"]["count"] == 31 and out["contacts"][0]["email"] == "sam@example.com"
    assert "thumbnail" not in out["contacts"][0]


async def test_get_contact_labels(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/contacts/4/labels", {"payload": ["vip", "refund"]})
    assert await call(make_client(), "get_contact_labels", contact_id=4) == {
        "contact_id": 4,
        "labels": ["vip", "refund"],
    }


async def test_get_conversation_labels(chatwoot, make_client):
    chatwoot.add("GET", f"{ACCOUNT}/conversations/12/labels", {"payload": ["bug"]})
    out = await call(make_client(), "get_conversation_labels", conversation_id=12)
    assert out == {"conversation_id": 12, "labels": ["bug"]}


async def test_get_inbox_keeps_settings_but_never_secrets_or_webhook_addresses(chatwoot, make_client):
    inbox = {
        "id": 3,
        "name": "Web",
        "channel_type": "Channel::WebWidget",
        "website_url": "https://shop.example.com",
        "greeting_enabled": True,
        "greeting_message": "Hi!",
        "working_hours_enabled": True,
        "timezone": "UTC",
        "csat_survey_enabled": False,
        "enable_auto_assignment": True,
        "allow_messages_after_resolved": True,
        "lock_to_single_conversation": False,
        "business_name": "Shop",
        "callback_webhook_url": "https://hooks.example.com/SECRET",
        "hmac_mandatory": True,
        "web_widget_script": "<script>SECRET</script>",
        "allowed_domains": "shop.example.com",
    }
    chatwoot.add("GET", f"{ACCOUNT}/inboxes/3", inbox)
    out = await call(make_client(), "get_inbox", inbox_id=3)
    assert out["greeting_message"] == "Hi!" and out["timezone"] == "UTC" and out["business_name"] == "Shop"
    assert "SECRET" not in str(out)
    assert not {"callback_webhook_url", "hmac_mandatory", "web_widget_script"} & set(out)


async def test_list_team_members(chatwoot, make_client):
    agent = {
        "id": 8,
        "name": "Ana",
        "email": "ana@example.com",
        "role": "agent",
        "availability_status": "online",
        "custom_attributes": {"x": 1},
    }
    chatwoot.add("GET", f"{ACCOUNT}/teams/2/team_members", [agent])
    out = await call(make_client(), "list_team_members", team_id=2)
    assert out["team_id"] == 2
    assert out["members"] == [
        {"id": 8, "name": "Ana", "email": "ana@example.com", "role": "agent", "availability_status": "online"}
    ]


@pytest.mark.parametrize(
    ("model", "wire"), [("conversation", "conversation_attribute"), ("contact", "contact_attribute")]
)
async def test_list_custom_attribute_definitions(chatwoot, make_client, model, wire):
    row = {
        "id": 1,
        "attribute_display_name": "Plan",
        "attribute_key": "plan",
        "attribute_display_type": "list",
        "attribute_description": "d",
        "attribute_values": ["free", "pro"],
        "attribute_model": wire,
        "regex_pattern": None,
        "created_at": "x",
    }
    chatwoot.add("GET", f"{ACCOUNT}/custom_attribute_definitions", [row])
    out = await call(make_client(), "list_custom_attribute_definitions", model=model)
    assert dict(chatwoot.last().url.params) == {"attribute_model": wire}
    assert out["custom_attributes"][0]["attribute_key"] == "plan" and out["custom_attributes"][0][
        "attribute_values"
    ] == ["free", "pro"]
    assert "created_at" not in out["custom_attributes"][0]


async def test_get_account_never_shows_billing_or_internal_settings(chatwoot, make_client):
    account = {
        "id": 5,
        "name": "Main",
        "locale": "en",
        "status": "active",
        "domain": "chat.example.com",
        "support_email": "help@example.com",
        "created_at": "2026-01-01",
        "custom_attributes": {"plan_name": "Enterprise", "subscription_status": "active"},
        "settings": {"auto_resolve_after": 10},
        "cache_keys": {"inbox": 1},
        "features": ["a", "b"],
        "billing_provider": "stripe",
        "latest_chatwoot_version": "4.0",
    }
    chatwoot.add("GET", ACCOUNT, account)
    out = await call(make_client(), "get_account")
    assert out == {
        "id": 5,
        "name": "Main",
        "locale": "en",
        "status": "active",
        "domain": "chat.example.com",
        "support_email": "help@example.com",
        "created_at": "2026-01-01",
    }


async def test_report_by_group_calls_the_right_report_with_dates(chatwoot, make_client):
    chatwoot.add("GET", f"{V2}/summary_reports/agent", [{"id": 8, "name": "Ana", "conversations_count": 4}])
    out = await call(
        make_client(),
        "get_report_by_group",
        group_by="agent",
        since="2026-10-01",
        until="2026-10-07",
        business_hours=True,
    )
    assert dict(chatwoot.last().url.params) == {"since": "1790812800", "until": "1791417599", "business_hours": "true"}
    assert out["rows"] == [{"id": 8, "name": "Ana", "conversations_count": 4}]


@pytest.mark.parametrize("group", ["agent", "team", "inbox", "label", "channel"])
async def test_every_group_has_its_own_report(chatwoot, make_client, group):
    chatwoot.add("GET", f"{V2}/summary_reports/{group}", [])
    await call(make_client(), "get_report_by_group", group_by=group, since="2026-10-01", until="2026-10-07")
    assert chatwoot.last().url.path.endswith(f"/summary_reports/{group}")


async def test_report_rows_are_capped(chatwoot, make_client):
    chatwoot.add("GET", f"{V2}/summary_reports/inbox", [{"id": i} for i in range(6)])
    out = await call(
        make_client(CHATWOOT_MCP_MAX_ITEMS="4"),
        "get_report_by_group",
        group_by="inbox",
        since="2026-10-01",
        until="2026-10-07",
    )
    assert len(out["rows"]) == 4 and "Only the first 4" in out["truncated"]


async def test_the_channel_report_refuses_more_than_six_months_like_chatwoot_does(chatwoot, make_client):
    with pytest.raises(ToolError, match="six months"):
        await call(make_client(), "get_report_by_group", group_by="channel", since="2026-01-01", until="2026-09-01")
    assert chatwoot.requests == []


async def test_report_by_group_rejects_an_unknown_group(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(), "get_report_by_group", group_by="weather", since="2026-10-01", until="2026-10-07")
    assert chatwoot.requests == []


async def test_first_response_distribution(chatwoot, make_client):
    chatwoot.add("GET", f"{V2}/reports/first_response_time_distribution", {"web": {"0-1h": 3}})
    out = await call(make_client(), "get_first_response_distribution", since="2026-10-01", until="2026-10-07")
    assert dict(chatwoot.last().url.params) == {"since": "1790812800", "until": "1791417599"}
    assert out == {"web": {"0-1h": 3}}
