"""Small lists that tell the assistant the names and ids it needs for other tools."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp import shaping
from chatwoot_mcp.tools.support import READ_ONLY, TIER_READ, ToolContext, reporting_errors


def _pick(item: Any, *fields: str) -> dict[str, Any]:
    return {field: item.get(field) for field in fields} if isinstance(item, dict) else {}


def _rows(data: Any) -> list[Any]:
    """Chatwoot answers some lists as a bare array and some as {"payload": [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("payload"), list):
        return data["payload"]
    return []


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    shown = ctx.settings.max_items
    text_limit = ctx.settings.max_text_chars

    async def listing(path: str, key: str, *fields: str) -> dict[str, Any]:
        items, cut = shaping.limit_items(_rows(await ctx.client.get_account(path)), shown)
        out: dict[str, Any] = {key: [_pick(item, *fields) for item in items]}
        if cut:
            out["truncated"] = f"Only the first {shown} items are shown."
        return out

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def list_inboxes() -> dict[str, Any]:
        """List the inboxes (website chat, email, WhatsApp and so on) with their ids."""
        return await listing("/inboxes", "inboxes", "id", "name", "channel_type", "website_url", "phone_number")

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def list_agents() -> dict[str, Any]:
        """List the support staff with their ids, roles and availability."""
        return await listing("/agents", "agents", "id", "name", "email", "role", "availability_status")

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def list_teams() -> dict[str, Any]:
        """List the teams with their ids."""
        return await listing("/teams", "teams", "id", "name", "description")

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def list_labels() -> dict[str, Any]:
        """List the labels (tags) that can be put on chats."""
        return await listing("/labels", "labels", "id", "title", "description")

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def list_canned_responses() -> dict[str, Any]:
        """List the saved quick replies: their short code and text."""
        out = await listing("/canned_responses", "canned_responses", "id", "short_code", "content")
        for row in out["canned_responses"]:
            if isinstance(row.get("content"), str):
                row["content"] = row["content"][:text_limit]
        return out

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def get_inbox(
        inbox_id: Annotated[int, Field(ge=1, description="The inbox id. See list_inboxes.")],
    ) -> dict[str, Any]:
        """Show one inbox's settings: greeting, working hours, auto-assignment and survey switches."""
        data = await ctx.client.get_account(f"/inboxes/{inbox_id}")
        return _pick(
            data,
            "id",
            "name",
            "channel_type",
            "website_url",
            "business_name",
            "greeting_enabled",
            "greeting_message",
            "working_hours_enabled",
            "timezone",
            "csat_survey_enabled",
            "enable_auto_assignment",
            "allow_messages_after_resolved",
            "lock_to_single_conversation",
        )

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def list_team_members(
        team_id: Annotated[int, Field(ge=1, description="The team id. See list_teams.")],
    ) -> dict[str, Any]:
        """List the agents in one team."""
        out = await listing(
            f"/teams/{team_id}/team_members", "members", "id", "name", "email", "role", "availability_status"
        )
        return {"team_id": team_id, **out}

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def list_custom_attribute_definitions(
        model: Annotated[
            Literal["conversation", "contact"], Field(description="Fields on chats or on customers.")
        ] = "conversation",
    ) -> dict[str, Any]:
        """List the custom fields the team has defined, with their keys and allowed values."""
        params = {"attribute_model": f"{model}_attribute"}
        items, cut = shaping.limit_items(
            _rows(await ctx.client.get_account("/custom_attribute_definitions", params=params)), shown
        )
        fields = (
            "id",
            "attribute_key",
            "attribute_display_name",
            "attribute_display_type",
            "attribute_description",
            "attribute_values",
            "attribute_model",
        )
        out: dict[str, Any] = {"custom_attributes": [_pick(item, *fields) for item in items]}
        if cut:
            out["truncated"] = f"Only the first {shown} items are shown."
        return out

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:lookups"})
    @reporting_errors
    async def get_account() -> dict[str, Any]:
        """Show the account's name, language, status and support address. No billing or settings."""
        data = await ctx.client.get_account("")
        return _pick(data, "id", "name", "locale", "status", "domain", "support_email", "created_at")
