"""Numbers: the report summary."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from chatwoot_mcp.tools.support import READ_ONLY, TIER_READ, ToolContext, reporting_errors

ReportType = Literal["account", "agent", "inbox", "label", "team"]
MAX_DAYS = 366


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
