"""Messages, write: ``add_private_note``.

A private note never reaches the customer: the tool asks for ``private: true`` and reports a problem if Chatwoot's
answer does not say private, so it never claims success for something that might have been sent. Replies to the
customer are in ``replies``, behind a confirm step.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Message
from chatwoot_mcp.shaping import shape_message
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import CREATE, ConversationId, ToolContext

MODULE = "messages"
MAX_NOTE_CHARS = 5000


def register(mcp: FastMCP, context: ToolContext) -> None:
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
