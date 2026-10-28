"""Numbers: report summaries, and what customers said in satisfaction surveys."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from chatwoot_mcp import shaping
from chatwoot_mcp.tools.support import READ_ONLY, TIER_READ, ToolContext, page_info, reporting_errors

ReportType = Literal["account", "agent", "inbox", "label", "team"]
GroupBy = Literal["agent", "team", "inbox", "label", "channel"]
MAX_DAYS = 366
# Chatwoot refuses a channel report longer than six calendar months (6.months = 15,778,476 seconds).
CHANNEL_MAX_SECONDS = 15_778_476


def day_range(since: str, until: str) -> tuple[int, int]:
    """Turn two YYYY-MM-DD dates (UTC) into Chatwoot's start-of-first-day and end-of-last-day timestamps."""
    try:
        first, last = date.fromisoformat(since), date.fromisoformat(until)
    except ValueError:
        raise ToolError("Dates must look like 2026-10-01 (year-month-day).") from None
    if last < first:
        raise ToolError("`until` must not be earlier than `since`.")
    if (last - first).days >= MAX_DAYS:
        raise ToolError(f"Choose a range of at most {MAX_DAYS} days.")
    start = int(datetime.combine(first, time.min, UTC).timestamp())
    end = int(datetime.combine(last, time.max.replace(microsecond=0), UTC).timestamp())
    return start, end


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:reports"})
    @reporting_errors
    async def get_report_summary(
        since: Annotated[str, Field(description="First day, as year-month-day in UTC, for example 2026-10-01.")],
        until: Annotated[str, Field(description="Last day (included), as year-month-day in UTC.")],
        type: Annotated[ReportType, Field(description="What to report on.")] = "account",  # noqa: A002
        id: Annotated[  # noqa: A002
            int | None, Field(ge=1, description="The agent, inbox, label or team id. Required unless type is account.")
        ] = None,
        business_hours: Annotated[bool, Field(description="Count only business hours.")] = False,
    ) -> dict[str, Any]:
        """Summarise chats for a period: how many, reply times, resolution times and satisfaction scores.

        The answer also carries the same numbers for the period just before, for comparison.
        """
        if type != "account" and id is None:
            raise ToolError(
                f"`id` is required when type is {type}. Use list_agents, list_inboxes, list_teams or list_labels."
            )
        start, end = day_range(since, until)
        params = {"type": type, "id": id, "since": start, "until": end, "business_hours": business_hours}
        data = await ctx.client.get_account("/reports/summary", params=params, version=2)
        return data if isinstance(data, dict) else {"result": data}

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:reports"})
    @reporting_errors
    async def get_report_by_group(
        group_by: Annotated[GroupBy, Field(description="Split the numbers by agent, team, inbox, label or channel.")],
        since: Annotated[str, Field(description="First day, as year-month-day in UTC, for example 2026-10-01.")],
        until: Annotated[str, Field(description="Last day (included), as year-month-day in UTC.")],
        business_hours: Annotated[bool, Field(description="Count only business hours.")] = False,
    ) -> dict[str, Any]:
        """Compare agents, teams, inboxes, labels or channels: chats handled, reply and resolution times."""
        start, end = day_range(since, until)
        if group_by == "channel" and end - start > CHANNEL_MAX_SECONDS:
            raise ToolError("The channel report covers at most six months. Choose a shorter period.")
        params = {"since": start, "until": end, "business_hours": business_hours}
        data = await ctx.client.get_account(f"/summary_reports/{group_by}", params=params, version=2)
        if not isinstance(data, list):
            return data if isinstance(data, dict) else {"rows": []}
        rows, cut = shaping.limit_items(data, ctx.settings.max_items)
        out: dict[str, Any] = {"rows": rows}
        if cut:
            out["truncated"] = f"Only the first {ctx.settings.max_items} rows are shown."
        return out

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:reports"})
    @reporting_errors
    async def get_first_response_distribution(
        since: Annotated[str, Field(description="First day, as year-month-day in UTC, for example 2026-10-01.")],
        until: Annotated[str, Field(description="Last day (included), as year-month-day in UTC.")],
    ) -> dict[str, Any]:
        """Show how quickly first replies went out, in time bands, for each channel."""
        start, end = day_range(since, until)
        params = {"since": start, "until": end}
        data = await ctx.client.get_account("/reports/first_response_time_distribution", params=params, version=2)
        return data if isinstance(data, dict) else {"result": data}

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:reports"})
    @reporting_errors
    async def list_csat_responses(
        since: Annotated[str, Field(description="First day, as year-month-day in UTC, for example 2026-10-01.")],
        until: Annotated[str, Field(description="Last day (included), as year-month-day in UTC.")],
        ratings: Annotated[
            list[Annotated[int, Field(ge=1, le=5)]] | None, Field(description="Only these scores, 1 (worst) to 5.")
        ] = None,
        agent_id: Annotated[int | None, Field(ge=1, description="Only chats this agent handled.")] = None,
        inbox_id: Annotated[int | None, Field(ge=1, description="Only chats in this inbox.")] = None,
        team_id: Annotated[int | None, Field(ge=1, description="Only chats of this team.")] = None,
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
    ) -> dict[str, Any]:
        """Read satisfaction-survey answers: the score and what the customer wrote, newest first. 25 per page."""
        start, end = day_range(since, until)
        params = {
            "since": start,
            "until": end,
            "rating": ratings,
            "user_ids": [agent_id] if agent_id else None,
            "inbox_id": inbox_id,
            "team_id": team_id,
            "page": page,
        }
        items, cut = shaping.limit_items(
            shaping.rows(await ctx.client.get_account("/csat_survey_responses", params=params)), ctx.settings.max_items
        )
        return {
            **page_info(page, cut, ctx.settings.max_items),
            "responses": [
                {
                    "id": r.get("id"),
                    "rating": r.get("rating"),
                    "feedback": shaping.wrap_untrusted(r.get("feedback_message"), ctx.settings.max_text_chars),
                    "conversation_id": r.get("conversation_id"),
                    "contact": shaping.person(r.get("contact"), "id", "name"),
                    "agent": shaping.person(r.get("assigned_agent"), "id", "name"),
                    "created_at": r.get("created_at"),
                }
                for r in items
                if isinstance(r, dict)
            ],
        }
