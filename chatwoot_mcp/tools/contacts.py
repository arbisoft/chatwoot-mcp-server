"""Reading customers: find one, open one, see their chats."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp import shaping
from chatwoot_mcp.tools.support import READ_ONLY, TIER_READ, ToolContext, page_info, reporting_errors

ContactId = Annotated[int, Field(ge=1, description="The customer's id in Chatwoot. Use search_contacts to find it.")]


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    shown = ctx.settings.max_items
    text_limit = ctx.settings.max_text_chars

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:contacts"})
    @reporting_errors
    async def search_contacts(
        query: Annotated[str, Field(min_length=1, max_length=200, description="A name, email, phone or identifier.")],
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
    ) -> dict[str, Any]:
        """Find customers by name, email, phone number or identifier."""
        data = await ctx.client.get_account("/contacts/search", params={"q": query, "page": page})
        items, cut = shaping.limit_items(data.get("payload") if isinstance(data, dict) else None, shown)
        return {
            **page_info(page, cut, shown),
            "meta": data.get("meta") if isinstance(data, dict) else None,
            "contacts": [shaping.compact_contact(c) for c in items],
        }

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:contacts"})
    @reporting_errors
    async def get_contact(contact_id: ContactId) -> dict[str, Any]:
        """Open one customer: name, email, phone and custom attributes."""
        data = await ctx.client.get_account(f"/contacts/{contact_id}")
        contact = data.get("payload", data) if isinstance(data, dict) else {}
        return shaping.compact_contact(contact)

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:contacts"})
    @reporting_errors
    async def get_contact_conversations(contact_id: ContactId) -> dict[str, Any]:
        """List every chat one customer has had."""
        data = await ctx.client.get_account(f"/contacts/{contact_id}/conversations")
        items, cut = shaping.limit_items(data.get("payload") if isinstance(data, dict) else None, shown)
        return {
            "contact_id": contact_id,
            **page_info(1, cut, shown),
            "conversations": [shaping.compact_conversation(c, text_limit) for c in items],
        }

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:contacts"})
    @reporting_errors
    async def list_contacts(
        page: Annotated[int, Field(ge=1, description="Page number, starting at 1.")] = 1,
        labels: Annotated[list[str] | None, Field(description="Only customers with any of these labels.")] = None,
    ) -> dict[str, Any]:
        """Browse customers, 15 per page. To find a particular customer use search_contacts."""
        data = await ctx.client.get_account("/contacts", params={"page": page, "labels": labels})
        items, cut = shaping.limit_items(data.get("payload") if isinstance(data, dict) else None, shown)
        return {
            **page_info(page, cut, shown),
            "meta": data.get("meta") if isinstance(data, dict) else None,
            "contacts": [shaping.compact_contact(c) for c in items],
        }

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:contacts"})
    @reporting_errors
    async def get_contact_labels(contact_id: ContactId) -> dict[str, Any]:
        """List the labels on one customer."""
        data = await ctx.client.get_account(f"/contacts/{contact_id}/labels")
        return {"contact_id": contact_id, "labels": (data.get("payload") if isinstance(data, dict) else None) or []}
