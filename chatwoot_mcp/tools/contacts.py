"""Contacts (customers): find, browse, open one, their chats and the team's notes; and, unless read-only, create,
update and add a note. None of these writes reach the customer."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Contact, Conversation, Note, payload
from chatwoot_mcp.shaping import shape_contact, shape_conversation, shape_note
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import (
    CREATE,
    OVERWRITE,
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
MAX_NOTE_CHARS = 5000
PHONE = re.compile(r"\+[1-9][0-9]{6,14}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

Email = Annotated[str | None, Field(max_length=254, description="The customer's email address.")]
Phone = Annotated[str | None, Field(description="International format with no spaces, like +15550100.")]
Identifier = Annotated[str | None, Field(max_length=255, description="Your own id for this customer.")]


def contact_fields(email: str | None, phone_number: str | None, identifier: str | None) -> dict[str, str]:
    """The given contact fields; raise ``ValueError`` with a plain reason when one is malformed."""
    for name, value in (("email", email), ("identifier", identifier)):
        if value is not None and not value.strip():
            raise ValueError(f"the {name} is empty")
    if email is not None and not EMAIL.fullmatch(email):
        raise ValueError("the email does not look like an email address")
    if phone_number is not None and not PHONE.fullmatch(phone_number):
        raise ValueError("the phone number must be in international format with no spaces, like +15550100")
    given = {"email": email, "phone_number": phone_number, "identifier": identifier}
    return {key: value for key, value in given.items() if value is not None}


def register(mcp: FastMCP, context: ToolContext) -> None:
    def contact_list(data: Any, page: int) -> dict[str, Any]:
        items, cut = limited(Contact.parse_rows(data), context.max_items)
        return {
            **page_info(page, cut, context.max_items),
            "meta": as_dict(data).get("meta"),
            "contacts": [shape_contact(c, context.links) for c in items],
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
        labels: Annotated[list[str] | None, Field(description="Only customers with any of these labels.")] = None,
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

    if not context.writes_enabled:
        return

    @mcp.tool(**context.tool("Create contact", CREATE, module=MODULE, tier=Tier.AGENT))
    async def create_contact(
        name: Annotated[str, Field(min_length=1, max_length=200, description="The customer's name.")],
        email: Email = None,
        phone_number: Phone = None,
        identifier: Identifier = None,
    ) -> dict[str, Any] | str:
        """Create a customer. Give at least one way to reach them: email, phone_number or identifier."""
        if not name.strip():
            return "Error: the name is empty"
        try:
            fields = contact_fields(email, phone_number, identifier)
        except ValueError as error:
            return f"Error: {error}"
        if not fields:
            return "Error: give at least one of email, phone_number or identifier"
        try:
            session = await context.session()
            created = payload(await session.post("/contacts", {"name": name, **fields}))
            contact = Contact.parse(created.get("contact"))
        except ChatwootError as error:
            return to_tool_error(error)
        return shape_contact(contact, context.links)

    @mcp.tool(**context.tool("Update contact", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def update_contact(
        contact_id: ContactId,
        name: Annotated[str | None, Field(min_length=1, max_length=200, description="The new name.")] = None,
        email: Email = None,
        phone_number: Phone = None,
        identifier: Identifier = None,
    ) -> dict[str, Any] | str:
        """Change a customer's name, email, phone number or identifier. Only the fields you give are changed."""
        if name is not None and not name.strip():
            return "Error: the name is empty"
        try:
            fields = contact_fields(email, phone_number, identifier)
        except ValueError as error:
            return f"Error: {error}"
        if name is not None:
            fields = {"name": name, **fields}
        if not fields:
            return "Error: give at least one field to change"
        try:
            session = await context.session()
            contact = Contact.parse(payload(await session.put(f"/contacts/{contact_id}", fields)))
        except ChatwootError as error:
            return to_tool_error(error)
        return shape_contact(contact, context.links)

    @mcp.tool(**context.tool("Add contact note", CREATE, module=MODULE, tier=Tier.AGENT))
    async def add_contact_note(
        contact_id: ContactId,
        content: Annotated[
            str, Field(min_length=1, max_length=MAX_NOTE_CHARS, description="The note text. Only the team sees it.")
        ],
    ) -> dict[str, Any] | str:
        """Add a note about a customer, shown to the team next to every chat with them. Never sent to the customer."""
        if not content.strip():
            return "Error: the note is empty"
        try:
            session = await context.session()
            note = Note.parse(await session.post(f"/contacts/{contact_id}/notes", {"note": {"content": content}}))
        except ChatwootError as error:
            return to_tool_error(error)
        return {"contact_id": contact_id, **shape_note(note, max_chars=context.max_chars)}
