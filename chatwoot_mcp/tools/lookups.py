"""Small lists that tell the assistant the names and ids it needs for other tools."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

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
