import json

import pytest

from chatwoot_mcp.tools.conversations import snooze_seconds
from tests.helpers import data, unframed

CHAT = {
    "id": 12,
    "status": "open",
    "custom_attributes": {"keep_me": "yes"},
    "meta": {"channel": "Channel::Email", "sender": {"id": 4, "name": "Sam"}},
}
LIST = {"data": {"meta": {"all_count": 1}, "payload": [CHAT]}}


async def test_list_conversations_sends_the_filters_and_shapes_the_answer(settings, fake):
    fake.on("GET", "/conversations", json=LIST)
    out = await data(settings, fake, "list_conversations", {"status": "all", "labels": ["vip", "bug"], "page": 2})
    query = fake.calls("GET", "/conversations")[-1].url.params
    assert query["status"] == "all" and query.get_list("labels[]") == ["vip", "bug"] and query["page"] == "2"
    assert out["page"] == 2 and out["counts"] == {"all_count": 1}
    assert unframed(out["conversations"][0]["contact"]) == {"id": 4, "name": "Sam"}


async def test_a_list_longer_than_the_limit_says_it_was_cut(settings, fake):
    import dataclasses

    fake.on("GET", "/conversations", json={"data": {"payload": [{"id": i} for i in range(1, 4)]}})
    out = await data(dataclasses.replace(settings, max_items=2), fake, "list_conversations")
    assert [c["id"] for c in out["conversations"]] == [1, 2] and "first 2" in out["truncated"]


async def test_search_uses_q(settings, fake):
    fake.on("GET", "/conversations", json=LIST)
    await data(settings, fake, "search_conversations", {"query": "refund"})
    assert fake.calls("GET", "/conversations")[-1].url.params["q"] == "refund"


async def test_filter_turns_conditions_into_chatwoots_query_with_no_join_after_the_last(settings, fake):
    fake.on("POST", "/conversations/filter", json={"meta": {"all_count": 1}, "payload": [CHAT]})
    out = await data(
        settings,
        fake,
        "filter_conversations",
        {
            "conditions": [
                {"field": "assignee_id", "operator": "is_not_present"},
                {"field": "last_activity_at", "operator": "days_before", "values": [3]},
            ],
            "match": "any",
        },
    )
    assert fake.last_json("POST", "/conversations/filter") == {
        "payload": [
            {"attribute_key": "assignee_id", "filter_operator": "is_not_present", "values": [], "query_operator": "or"},
            {
                "attribute_key": "last_activity_at",
                "filter_operator": "days_before",
                "values": [3],
                "query_operator": None,
            },
        ],
        "page": 1,
    }
    assert out["conversations"][0]["id"] == 12


async def test_get_conversation_and_counts(settings, fake):
    fake.on("GET", "/conversations/12", json=CHAT)
    fake.on("GET", "/conversations/meta", json={"meta": {"mine_count": 1}})
    assert (await data(settings, fake, "get_conversation", {"conversation_id": 12}))["url"].endswith(
        "/conversations/12"
    )
    assert await data(settings, fake, "get_conversation_counts") == {"mine_count": 1}


async def test_a_missing_conversation_is_a_plain_error(settings, fake):
    assert (await data(settings, fake, "get_conversation", {"conversation_id": 99})).startswith("Error: not found")


async def test_attachments_list_no_download_links(settings, fake):
    file = {"id": 3, "message_id": 40, "file_type": "image", "data_url": "https://signed", "sender": {"id": 4}}
    fake.on("GET", "/conversations/12/attachments", json={"meta": {"total_count": 1}, "payload": [file]})
    out = await data(settings, fake, "list_conversation_attachments", {"conversation_id": 12})
    assert out["total_count"] == 1 and "signed" not in json.dumps(out)


# --- writes ---------------------------------------------------------------------------------------


async def test_assign_sets_the_team_first_so_the_team_change_cannot_drop_the_agent(settings, fake):
    fake.on("POST", "/conversations/12/assignments", json={})
    out = await data(settings, fake, "assign_conversation", {"conversation_id": 12, "agent_id": 8, "team_id": 2})
    bodies = [json.loads(r.content) for r in fake.calls("POST", "/conversations/12/assignments")]
    assert bodies == [{"team_id": 2}, {"assignee_id": 8}]
    assert out == {"conversation_id": 12, "assigned_agent_id": 8, "assigned_team_id": 2}


async def test_assign_needs_someone_and_says_when_only_the_team_was_set(settings, fake):
    assert (await data(settings, fake, "assign_conversation", {"conversation_id": 12})).startswith("Error: give")
    answers = iter([200, 422])

    def handler(request):
        from httpx import Response

        return Response(next(answers), json={"message": "Agent is not an inbox member"})

    fake.on("POST", "/conversations/12/assignments", handler=handler)
    out = await data(settings, fake, "assign_conversation", {"conversation_id": 12, "agent_id": 8, "team_id": 2})
    assert out.startswith("Error: the team was assigned, but assigning the agent failed") and "inbox member" in out


async def test_snoozing_sends_unix_seconds_because_chatwoot_reads_strptime_s(settings, fake):
    fake.on("POST", "/conversations/12/toggle_status", json={"payload": {"current_status": "snoozed"}})
    await data(
        settings,
        fake,
        "set_conversation_status",
        {"conversation_id": 12, "status": "snoozed", "snoozed_until": "2099-01-01T09:00:00+00:00"},
    )
    assert fake.last_json("POST", "/conversations/12/toggle_status") == {
        "status": "snoozed",
        "snoozed_until": 4070941200,
    }


@pytest.mark.parametrize(
    ("status", "until", "reason"),
    [
        ("snoozed", None, "needs `snoozed_until`"),
        ("snoozed", "2099-01-01T09:00:00", "time zone"),
        ("snoozed", "tomorrow", "time zone"),
        ("snoozed", "2020-01-01T00:00:00+00:00", "future"),
        ("open", "2099-01-01T09:00:00+00:00", "only with status snoozed"),
    ],
)
async def test_bad_snooze_times_never_reach_chatwoot(settings, fake, status, until, reason):
    out = await data(
        settings, fake, "set_conversation_status", {"conversation_id": 12, "status": status, "snoozed_until": until}
    )
    assert out.startswith("Error:") and reason in out
    assert fake.writes() == []
    assert snooze_seconds("resolved", None) is None


async def test_priority_none_clears_it(settings, fake):
    fake.on("POST", "/conversations/12/toggle_priority", json={})
    await data(settings, fake, "set_conversation_priority", {"conversation_id": 12, "priority": "none"})
    assert fake.last_json("POST", "/conversations/12/toggle_priority") == {"priority": None}


async def test_custom_fields_keep_the_others_and_refuse_unknown_keys(settings, fake):
    fake.on("GET", "/custom_attribute_definitions", json=[{"id": 1, "attribute_key": "order_number"}])
    fake.on("GET", "/conversations/12", json=CHAT)
    fake.on("POST", "/conversations/12/custom_attributes", json={"custom_attributes": {"order_number": "B2"}})
    out = await data(
        settings,
        fake,
        "set_conversation_custom_attributes",
        {"conversation_id": 12, "attributes": {"order_number": "B2"}},
    )
    assert fake.last_json("POST", "/conversations/12/custom_attributes") == {
        "custom_attributes": {"keep_me": "yes", "order_number": "B2"}
    }
    assert unframed(out["custom_attributes"]) == {"order_number": "B2"}
    refused = await data(
        settings, fake, "set_conversation_custom_attributes", {"conversation_id": 12, "attributes": {"made_up": 1}}
    )
    assert "do not exist: made_up" in refused and len(fake.calls("POST", "/conversations/12/custom_attributes")) == 1


RESOLVABLE = {"id": 12, "status": "open", "inbox_id": 3, "meta": {"sender": {"id": 4, "name": "Sam"}}}


@pytest.mark.parametrize(("survey", "warned"), [(True, True), (False, False)])
async def test_resolving_is_previewed_with_the_survey_warning_then_done_once(settings, fake, survey, warned):
    from fastmcp import Client

    from chatwoot_mcp.server import build_server

    fake.on("GET", "/conversations/12", json=RESOLVABLE)
    fake.on("GET", "/inboxes/3", json={"id": 3, "name": "Web", "csat_survey_enabled": survey})
    fake.on("POST", "/conversations/12/toggle_status", json={"payload": {"current_status": "resolved"}})
    async with Client(build_server(settings, transport=fake)) as client:
        first = (await client.call_tool("prepare_resolve_conversation", {"conversation_id": 12})).data
        assert fake.writes() == []
        token = first["confirmation_token"]
        done = await client.call_tool("resolve_conversation", {"conversation_id": 12, "confirmation_token": token})
        again = await client.call_tool("resolve_conversation", {"conversation_id": 12, "confirmation_token": token})
    assert ("satisfaction survey" in first["preview"].get("warning", "")) is warned
    assert done.data == {"conversation_id": 12, "status": "resolved"} and "already used" in again.data
    assert fake.last_json("POST", "/conversations/12/toggle_status") == {"status": "resolved"}
    assert len(fake.calls("POST", "/conversations/12/toggle_status")) == 1


async def test_a_resolved_chat_is_not_prepared_again_and_status_cannot_resolve(settings, fake):
    fake.on("GET", "/conversations/12", json={**RESOLVABLE, "status": "resolved"})
    assert "already resolved" in await data(settings, fake, "prepare_resolve_conversation", {"conversation_id": 12})
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="literal_error|Input should be"):
        await data(settings, fake, "set_conversation_status", {"conversation_id": 12, "status": "resolved"})
    assert fake.writes() == []


async def test_search_applies_the_status_itself_because_chatwoot_ignores_it_with_a_query(settings, fake):
    rows = [{"id": 1, "status": "open"}, {"id": 2, "status": "resolved"}]
    fake.on("GET", "/conversations", json={"data": {"meta": {}, "payload": rows}})
    out = await data(settings, fake, "search_conversations", {"query": "refund", "status": "open"})
    assert [c["id"] for c in out["conversations"]] == [1]
    assert "status" not in fake.calls("GET", "/conversations")[-1].url.params


@pytest.mark.parametrize(("page", "chatwoot_page", "first_id", "next_page"), [(2, 1, 26, 3), (5, 2, 101, None)])
async def test_every_attachment_is_reachable_25_at_a_time(settings, fake, page, chatwoot_page, first_id, next_page):
    def handler(request):
        from httpx import Response

        start = (int(request.url.params["page"]) - 1) * 100
        rows = [{"id": n, "file_type": "image"} for n in range(start + 1, min(start + 100, 110) + 1)]
        return Response(200, json={"meta": {"total_count": 110}, "payload": rows})

    fake.on("GET", "/conversations/12/attachments", handler=handler)
    out = await data(settings, fake, "list_conversation_attachments", {"conversation_id": 12, "page": page})
    assert fake.calls("GET", "/conversations/12/attachments")[-1].url.params["page"] == str(chatwoot_page)
    assert out["attachments"][0]["id"] == first_id and out.get("next_page") == next_page
