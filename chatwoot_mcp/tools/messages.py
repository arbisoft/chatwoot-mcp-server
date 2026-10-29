"""Messages: ``get_messages``, and unless read-only ``add_private_note``.

A private note never reaches the customer: the tool asks for ``private: true`` and reports a problem if Chatwoot's
answer does not say private, so it never claims success for something that might have been sent. Replies to the
customer are in ``replies``, behind a confirm step.

Chatwoot returns the latest 20 messages when no ids are given, up to 20 before ``before``, and up to 100 after
``after``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Message
from chatwoot_mcp.shaping import shape_message
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import CREATE, READ, ConversationId, ToolContext, as_dict, limited

MODULE = "messages"
MAX_NOTE_CHARS = 5000


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Get messages", READ, module=MODULE, tier=Tier.AGENT))
    async def get_messages(
        conversation_id: ConversationId,
        before: Annotated[
            int | None, Field(ge=1, description="Only messages older than this message id (to read further back).")
        ] = None,
        after: Annotated[int | None, Field(ge=1, description="Only messages newer than this message id.")] = None,
    ) -> dict[str, Any] | str:
        """Read a chat's messages, oldest first. With no ids, the latest 20; pass `before` to read further back."""
        try:
            session = await context.session()
            data = await session.get(f"/conversations/{conversation_id}/messages", {"before": before, "after": after})
            items, cut = limited(Message.parse_rows(data), context.max_items)
        except ChatwootError as error:
            return to_tool_error(error)
        out: dict[str, Any] = {
            "conversation_id": conversation_id,
            "labels": as_dict(as_dict(data).get("meta")).get("labels") or [],
            "messages": [shape_message(m, max_chars=context.max_chars) for m in items],
        }
        if cut:
            out["truncated"] = (
                f"Only the first {context.max_items} messages are shown. Call again with after={items[-1].id}."
            )
        return out

    if not context.writes_enabled:
        return

    @mcp.tool(**context.tool("Add private note", CREATE, module=MODULE, tier=Tier.AGENT))
    async def add_private_note(
        conversation_id: ConversationId,
        content: Annotated[
            str, Field(min_length=1, max_length=MAX_NOTE_CHARS, description="The note text. Only the team sees it.")
        ],
    ) -> dict[str, Any] | str:
        """Add an internal note to a chat. It is never sent to the customer."""
        if not content.strip():
            return "Error: the note is empty"
        body = {"content": content, "message_type": "outgoing", "private": True}
        try:
            session = await context.session()
            message = Message.parse(await session.post(f"/conversations/{conversation_id}/messages", body))
        except ChatwootError as error:
            return to_tool_error(error)
        if not message.private:
            # Never report success for something that might have reached the customer.
            return "Error: Chatwoot did not mark the message private; check the chat in Chatwoot right away"
        return shape_message(message, max_chars=context.max_chars)
