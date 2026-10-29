"""Reports: summaries, group comparisons, first-reply bands and satisfaction-survey answers. Administrators only
(or an Enterprise custom role with ``report_manage``), which is Chatwoot's own rule.

Report timestamps are Unix seconds; the tools take ``YYYY-MM-DD`` dates (UTC) and convert them. Some report routes
exist only in the Enterprise edition or in some versions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import CsatResponse
from chatwoot_mcp.shaping import shape_csat
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, ChatwootSession, ToolContext, as_dict, limited, page_info, tier_error

MODULE = "reports"
MAX_DAYS = 366
# Chatwoot refuses a channel report longer than six calendar months (6.months = 15,778,476 seconds).
CHANNEL_MAX_SECONDS = 15_778_476

ReportType = Literal["account", "agent", "inbox", "label", "team"]
GroupBy = Literal["agent", "team", "inbox", "label", "channel"]
Since = Annotated[str, Field(description="First day, as year-month-day in UTC, for example 2026-10-01.")]
Until = Annotated[str, Field(description="Last day (included), as year-month-day in UTC.")]


def day_range(since: str, until: str) -> tuple[int, int]:
    """Two YYYY-MM-DD dates (UTC) as start-of-first-day and end-of-last-day timestamps; ``ValueError`` if invalid."""
    try:
        first, last = date.fromisoformat(since), date.fromisoformat(until)
    except ValueError:
        raise ValueError("dates must look like 2026-10-01 (year-month-day)") from None
    if last < first:
        raise ValueError("`until` must not be earlier than `since`")
    if (last - first).days >= MAX_DAYS:
        raise ValueError(f"choose a range of at most {MAX_DAYS} days")
    start = int(datetime.combine(first, time.min, UTC).timestamp())
    end = int(datetime.combine(last, time.max.replace(microsecond=0), UTC).timestamp())
    return start, end


class ReportsRefused(ChatwootError):
    """The caller is not known to be an administrator; refused before calling Chatwoot (fail closed)."""

    def __init__(self, session: ChatwootSession) -> None:
        reason = tier_error(Tier.ADMIN, session.tier).removeprefix("Error: ")
        super().__init__(f"{reason} (reports need an administrator, or a custom role with report_manage)")


def require_admin(session: ChatwootSession) -> None:
    if session.lacks(Tier.ADMIN):
        raise ReportsRefused(session)


def register(mcp: FastMCP, context: ToolContext) -> None:
    async def report(path: str, params: dict[str, Any]) -> Any:
        session = await context.session()
        require_admin(session)
        return await session.get(path, params, version=2)

    @mcp.tool(**context.tool("Get report summary", READ, module=MODULE, tier=Tier.ADMIN))
    async def get_report_summary(
        since: Since,
        until: Until,
        type: Annotated[ReportType, Field(description="What to report on.")] = "account",  # noqa: A002
        id: Annotated[  # noqa: A002
            int | None, Field(ge=1, description="The agent, inbox, label or team id. Required unless type is account.")
        ] = None,
        business_hours: Annotated[bool, Field(description="Count only business hours.")] = False,
    ) -> dict[str, Any] | str:
        """Summarise chats for a period: how many, reply times, resolution times and satisfaction scores.

        The answer also carries the same numbers for the period just before, for comparison.
        """
        if type != "account" and id is None:
            return (
                f"Error: `id` is required when type is {type}; use list_agents, list_inboxes, list_teams or list_labels"
            )
        try:
            start, end = day_range(since, until)
        except ValueError as error:
            return f"Error: {error}"
        params = {"type": type, "id": id, "since": start, "until": end, "business_hours": business_hours}
        try:
            data = await report("/reports/summary", params)
        except ChatwootError as error:
            return to_tool_error(error)
        return as_dict(data)

    @mcp.tool(**context.tool("Get report by group", READ, module=MODULE, tier=Tier.ADMIN))
    async def get_report_by_group(
        group_by: Annotated[GroupBy, Field(description="Split the numbers by agent, team, inbox, label or channel.")],
        since: Since,
        until: Until,
        business_hours: Annotated[bool, Field(description="Count only business hours.")] = False,
    ) -> dict[str, Any] | str:
        """Compare agents, teams, inboxes, labels or channels: chats handled, reply and resolution times."""
        try:
            start, end = day_range(since, until)
        except ValueError as error:
            return f"Error: {error}"
        if group_by == "channel" and end - start > CHANNEL_MAX_SECONDS:
            return "Error: the channel report covers at most six months; choose a shorter period"
        try:
            data = await report(
                f"/summary_reports/{group_by}", {"since": start, "until": end, "business_hours": business_hours}
            )
        except ChatwootError as error:
            return to_tool_error(error)
        if not isinstance(data, list):
            return as_dict(data)
        shown, cut = limited(data, context.max_items)
        out: dict[str, Any] = {"rows": shown}
        if cut:
            out["truncated"] = f"Only the first {context.max_items} rows are shown."
        return out

    @mcp.tool(**context.tool("Get first response distribution", READ, module=MODULE, tier=Tier.ADMIN))
    async def get_first_response_distribution(since: Since, until: Until) -> dict[str, Any] | str:
        """Show how quickly first replies went out, in time bands, for each channel."""
        try:
            start, end = day_range(since, until)
        except ValueError as error:
            return f"Error: {error}"
        try:
            data = await report("/reports/first_response_time_distribution", {"since": start, "until": end})
        except ChatwootError as error:
            return to_tool_error(error)
        return as_dict(data)

    @mcp.tool(**context.tool("List CSAT responses", READ, module=MODULE, tier=Tier.ADMIN))
    async def list_csat_responses(
        since: Since,
        until: Until,
        ratings: Annotated[
            list[Annotated[int, Field(ge=1, le=5)]] | None, Field(description="Only these scores, 1 (worst) to 5.")
        ] = None,
        agent_id: Annotated[int | None, Field(ge=1, description="Only chats this agent handled.")] = None,
        inbox_id: Annotated[int | None, Field(ge=1, description="Only chats in this inbox.")] = None,
        team_id: Annotated[int | None, Field(ge=1, description="Only chats of this team.")] = None,
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
    ) -> dict[str, Any] | str:
        """Read satisfaction-survey answers: the score and what the customer wrote, newest first. 25 per page."""
        try:
            start, end = day_range(since, until)
        except ValueError as error:
            return f"Error: {error}"
        params = {
            "since": start,
            "until": end,
            "rating": ratings,
            "user_ids": [agent_id] if agent_id else None,
            "inbox_id": inbox_id,
            "team_id": team_id,
            "page": page,
        }
        try:
            session = await context.session()
            require_admin(session)
            items, cut = limited(
                CsatResponse.parse_rows(await session.get("/csat_survey_responses", params)), context.max_items
            )
        except ChatwootError as error:
            return to_tool_error(error)
        return {
            **page_info(page, cut, context.max_items),
            "responses": [shape_csat(r, max_chars=context.max_chars) for r in items],
        }
