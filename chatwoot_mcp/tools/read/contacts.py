"""Contacts (customers), read: find, browse, open one, their latest chats and the team's notes about them.

Chatwoot returns a customer's notes all at once, so ``get_contact_notes`` pages them itself, and only a customer's
latest 25 chats.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Contact, Conversation, Note, payload
from chatwoot_mcp.shaping import shape_contact, shape_conversation, shape_note, shape_report
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import (
    READ,
    ContactId,
    Page,
    ToolContext,
    as_dict,
    limited,
    local_page,
    page_info,
)

MODULE = "contacts"


def register(mcp: FastMCP, context: ToolContext) -> None:
    def contact_list(data: Any, page: int) -> dict[str, Any]:
        # Chatwoot pages contacts itself (15 per page); a page is returned whole so none of it is unreachable.
        return {
            "page": page,
            "meta": shape_report(as_dict(data).get("meta")),
            "contacts": [shape_contact(c, context.links) for c in Contact.parse_rows(data)],
        }

    @mcp.tool(**context.tool("Search contacts", READ, module=MODULE, tier=Tier.AGENT))
    async def search_contacts(
        query: Annotated[str, Field(min_length=1, max_length=200, description="A name, email, phone or identifier.")],
        page: Page = 1,
    ) -> dict[str, Any] | str:
        """Find customers by name, email, phone number or identifier."""
        try:
            session = await context.session()
            return contact_list(await session.get("/contacts/search", {"q": query, "page": page}), page)
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("List contacts", READ, module=MODULE, tier=Tier.AGENT))
    async def list_contacts(
        page: Page = 1,
        labels: Annotated[
            list[str] | None, Field(max_length=20, description="Only customers with any of these labels.")
        ] = None,
    ) -> dict[str, Any] | str:
        """Browse customers, 15 per page. To find a particular customer use search_contacts."""
        try:
            session = await context.session()
            return contact_list(await session.get("/contacts", {"page": page, "labels": labels}), page)
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("Get contact", READ, module=MODULE, tier=Tier.AGENT))
    async def get_contact(contact_id: ContactId) -> dict[str, Any] | str:
        """Open one customer: name, email, phone and custom attributes."""
        try:
            session = await context.session()
            contact = Contact.parse(payload(await session.get(f"/contacts/{contact_id}")))
        except ChatwootError as error:
            return to_tool_error(error)
        return shape_contact(contact, context.links)

    @mcp.tool(**context.tool("Get contact conversations", READ, module=MODULE, tier=Tier.AGENT))
    async def get_contact_conversations(contact_id: ContactId) -> dict[str, Any] | str:
        """List one customer's latest 25 chats, newest first (Chatwoot returns no more)."""
        try:
            session = await context.session()
            data = await session.get(f"/contacts/{contact_id}/conversations")
            items, cut = limited(Conversation.parse_rows(data), context.max_items)
        except ChatwootError as error:
            return to_tool_error(error)
        return {
            "contact_id": contact_id,
            **page_info(1, cut, context.max_items),
            "conversations": [shape_conversation(c, context.links, max_chars=context.max_chars) for c in items],
        }

    @mcp.tool(**context.tool("Get contact notes", READ, module=MODULE, tier=Tier.AGENT))
    async def get_contact_notes(contact_id: ContactId, page: Page = 1) -> dict[str, Any] | str:
        """Read the team's notes about one customer, newest first. Customers never see these."""
        try:
            session = await context.session()
            notes = Note.parse_rows(await session.get(f"/contacts/{contact_id}/notes"))
        except ChatwootError as error:
            return to_tool_error(error)
        items, info = local_page(notes, page, context.max_items)
        return {
            "contact_id": contact_id,
            **info,
            "notes": [shape_note(n, max_chars=context.max_chars) for n in items],
        }
