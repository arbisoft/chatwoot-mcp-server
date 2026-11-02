"""Messages, read: ``get_messages``.

Chatwoot returns the latest 20 messages when no ids are given, up to 20 before ``before``, and up to 100 after
``after``. With both, Chatwoot includes the ``after`` message itself, so the lower bound is moved up by one.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Message
from chatwoot_mcp.shaping import shape_message
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, ConversationId, ToolContext, as_dict, limited

MODULE = "messages"


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
            lower = after + 1 if after is not None and before is not None else after
            data = await session.get(f"/conversations/{conversation_id}/messages", {"before": before, "after": lower})
            items, cut = limited(Message.parse_rows(data), context.max_items)
        except ChatwootError as error:
            return to_tool_error(error)
        out: dict[str, Any] = {
            "conversation_id": conversation_id,
            "labels": as_dict(as_dict(data).get("meta")).get("labels") or [],
            "messages": [shape_message(m, max_chars=context.max_chars) for m in items],
        }
        if cut:
            again = f"after={items[-1].id}" + (f" and before={before}" if before is not None else "")
            out["truncated"] = f"Only the first {context.max_items} messages are shown. Call again with {again}."
        return out
