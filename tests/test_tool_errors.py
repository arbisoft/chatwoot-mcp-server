"""When Chatwoot fails, every tool answers with a plain ``Error: ...`` string, never a traceback or a raw payload."""

import httpx
import pytest
from fastmcp import Client

from chatwoot_mcp.server import build_server
from tests.conftest import ADMIN_PROFILE, RecordingTransport
from tests.test_tools import CONFIRMED_TOOLS, OPT_IN_READ_TOOLS, READ_TOOLS, WRITE_TOOLS

DAYS = {"since": "2026-10-01", "until": "2026-10-02"}
ARGUMENTS = {
    "get_inbox": {"inbox_id": 1},
    "list_team_members": {"team_id": 1},
    "search_conversations": {"query": "x"},
    "filter_conversations": {"conditions": [{"field": "status", "operator": "equal_to", "values": ["open"]}]},
    "get_conversation": {"conversation_id": 1},
    "list_conversation_attachments": {"conversation_id": 1},
    "get_messages": {"conversation_id": 1},
    "search_contacts": {"query": "x"},
    "get_contact": {"contact_id": 1},
    "get_contact_conversations": {"contact_id": 1},
    "get_contact_notes": {"contact_id": 1},
    "get_conversation_labels": {"conversation_id": 1},
    "get_contact_labels": {"contact_id": 1},
    "search_help_articles": {"query": "x"},
    "get_report_summary": DAYS,
    "get_report_by_group": {"group_by": "agent", **DAYS},
    "get_first_response_distribution": DAYS,
    "list_csat_responses": DAYS,
    "assign_conversation": {"conversation_id": 1, "team_id": 2},
    "set_conversation_status": {"conversation_id": 1, "status": "resolved"},
    "set_conversation_priority": {"conversation_id": 1, "priority": "low"},
    "set_conversation_custom_attributes": {"conversation_id": 1, "attributes": {"a": 1}},
    "add_private_note": {"conversation_id": 1, "content": "x"},
    "create_contact": {"name": "x", "email": "x@example.com"},
    "update_contact": {"contact_id": 1, "name": "x"},
    "add_contact_note": {"contact_id": 1, "content": "x"},
    "add_conversation_labels": {"conversation_id": 1, "labels": ["a"]},
    "add_contact_labels": {"contact_id": 1, "labels": ["a"]},
    "remove_conversation_labels": {"conversation_id": 1, "labels": ["a"]},
    "prepare_send_reply": {"conversation_id": 1, "content": "x"},
    "send_reply": {"conversation_id": 1, "content": "x", "confirmation_token": "t"},
    "prepare_create_conversation": {"contact_id": 1, "inbox_id": 1},
    "create_conversation": {"contact_id": 1, "inbox_id": 1, "confirmation_token": "t"},
    "prepare_run_macro": {"macro_id": 1, "conversation_id": 1},
    "run_macro": {"macro_id": 1, "conversation_id": 1, "confirmation_token": "t"},
}
EVERY_TOOL = sorted(READ_TOOLS | WRITE_TOOLS | OPT_IN_READ_TOOLS | CONFIRMED_TOOLS)
CONFIRMING = {"send_reply", "create_conversation", "run_macro"}
REPORTS = {"get_report_summary", "get_report_by_group", "get_first_response_distribution", "list_csat_responses"}


def failing_chatwoot(status: int, body) -> RecordingTransport:
    """The profile answers (so the caller is a known admin); every other route fails the same way."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/profile" and status != 401:
            return httpx.Response(200, json=ADMIN_PROFILE)
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="<html>oops")

    return RecordingTransport(handler)


async def call_all(settings, transport, names=EVERY_TOOL) -> dict[str, str]:
    results = {}
    async with Client(build_server(settings, transport=transport)) as client:
        for name in names:
            result = await client.call_tool(name, ARGUMENTS.get(name, {}), raise_on_error=False)
            results[name] = result.content[0].text if result.data is None else result.data
    return results


@pytest.mark.parametrize(
    ("status", "body", "needle"),
    [
        (500, {"error": "boom"}, "server error"),
        (404, {"error": "missing"}, "not found"),
        (401, {"error": "Invalid Access Token"}, "rejected the key"),
        (200, None, "not JSON"),
    ],
)
async def test_every_tool_turns_a_chatwoot_failure_into_a_plain_error(everything, status, body, needle):
    # get_me only reads the profile, which this fake answers unless the key is rejected.
    names = EVERY_TOOL if status == 401 else [name for name in EVERY_TOOL if name != "get_me"]
    results = await call_all(everything, failing_chatwoot(status, body), names)
    for name, result in results.items():
        assert isinstance(result, str) and result.startswith("Error:"), f"{name} answered {result!r}"
        assert "Traceback" not in result and "secret-token" not in result, name
        if name in CONFIRMING:
            continue
        if status == 401 and name in REPORTS:
            # The role cannot be read with a rejected key, so reports fail closed before calling Chatwoot.
            assert "role could not be determined" in result, f"{name}: {result}"
        else:
            assert needle in result, f"{name}: {result}"


async def test_the_confirming_tools_never_reach_chatwoot_without_a_valid_token(everything):
    transport = failing_chatwoot(500, {"error": "boom"})
    results = await call_all(everything, transport, sorted(CONFIRMING))
    for name in CONFIRMING:
        assert "unknown, already used or expired" in results[name], name
    assert [r for r in transport.requests if r.method != "GET"] == []
