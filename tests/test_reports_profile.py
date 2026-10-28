import pytest
from fastmcp.exceptions import ToolError

from chatwoot_mcp.tools.reports import day_range
from tests.conftest import call

V2 = "/api/v2/accounts/5"


def test_day_range_covers_whole_days_in_utc():
    assert day_range("2026-10-01", "2026-10-01") == (1790812800, 1790899199)
    assert day_range("2026-10-01", "2026-10-02")[1] - day_range("2026-10-01", "2026-10-02")[0] == 2 * 86400 - 1


@pytest.mark.parametrize(
    ("since", "until", "message"),
    [
        ("yesterday", "2026-10-02", "year-month-day"),
        ("2026-10-02", "2026-13-40", "year-month-day"),
        ("2026-10-05", "2026-10-01", "earlier"),
        ("2024-01-01", "2026-01-01", "at most 366"),
    ],
)
def test_day_range_refuses_bad_input(since, until, message):
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match=message):
        day_range(since, until)


async def test_report_summary_sends_timestamps_and_returns_chatwoots_numbers(chatwoot, make_client):
    chatwoot.add(
        "GET",
        f"{V2}/reports/summary",
        {"conversations_count": 7, "avg_first_response_time": "120", "previous": {"conversations_count": 5}},
    )
    out = await call(make_client(), "get_report_summary", since="2026-10-01", until="2026-10-07")
    assert out["conversations_count"] == 7 and out["previous"]["conversations_count"] == 5
    params = dict(chatwoot.last().url.params)
    assert params == {"type": "account", "since": "1790812800", "until": "1791417599", "business_hours": "false"}


async def test_report_summary_for_one_agent_needs_an_id(chatwoot, make_client):
    with pytest.raises(ToolError, match="`id` is required"):
        await call(make_client(), "get_report_summary", since="2026-10-01", until="2026-10-07", type="agent")
    assert chatwoot.requests == []
    chatwoot.add("GET", f"{V2}/reports/summary", {})
    await call(make_client(), "get_report_summary", since="2026-10-01", until="2026-10-07", type="agent", id=8)
    assert dict(chatwoot.last().url.params)["id"] == "8"


async def test_report_summary_rejects_a_bad_type(chatwoot, make_client):
    with pytest.raises(ToolError):
        await call(make_client(), "get_report_summary", since="2026-10-01", until="2026-10-07", type="everything")
    assert chatwoot.requests == []


PROFILE = {
    "id": 3,
    "name": "Habib",
    "email": "habib@example.com",
    "availability_status": "online",
    "access_token": "SECRET-ACCESS",
    "pubsub_token": "SECRET-PUBSUB",
    "accounts": [
        {"id": 5, "name": "Main", "role": "agent", "status": "active"},
        {"id": 6, "name": "Other", "role": "administrator"},
    ],
}


async def test_profile_never_shows_a_key_or_token(chatwoot, make_client):
    chatwoot.add("GET", "/api/v1/profile", PROFILE)
    out = await call(make_client(), "get_my_profile")
    text = str(out)
    assert "SECRET-ACCESS" not in text and "SECRET-PUBSUB" not in text and "access_token" not in text
    assert out["email"] == "habib@example.com"
    assert out["accounts"] == [
        {"id": 5, "name": "Main", "role": "agent"},
        {"id": 6, "name": "Other", "role": "administrator"},
    ]


async def test_profile_says_which_account_the_tools_use_and_whether_you_belong_to_it(chatwoot, make_client):
    chatwoot.add("GET", "/api/v1/profile", PROFILE)
    ok = await call(make_client(), "get_my_profile")
    assert ok["tools_work_in_account"] == 5 and ok["has_access_to_that_account"] is True
    other = await call(make_client(CHATWOOT_ACCOUNT_ID="9"), "get_my_profile")
    assert other["tools_work_in_account"] == 9 and other["has_access_to_that_account"] is False


async def test_profile_with_a_rejected_key_is_a_plain_error(chatwoot, make_client):
    chatwoot.add("GET", "/api/v1/profile", {}, status=401)
    with pytest.raises(ToolError, match="key was rejected"):
        await call(make_client(), "get_my_profile")
