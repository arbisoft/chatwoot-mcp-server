"""The two safe writes. Neither can reach a customer. Only registered when CHATWOOT_MCP_ENABLE_WRITES=true."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from chatwoot_mcp import shaping
from chatwoot_mcp.tools.support import SAFE_IDEMPOTENT_WRITE, SAFE_WRITE, TIER_WRITE, ToolContext, reporting_errors

MAX_NOTE_CHARS = 5000
MAX_LABELS_PER_CALL = 20
Labels = Annotated[
    list[str],
    Field(min_length=1, max_length=MAX_LABELS_PER_CALL, description="Existing label titles to add."),
]


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    text_limit = ctx.settings.max_text_chars

    @mcp.tool(annotations=SAFE_WRITE, tags={TIER_WRITE, "module:writes"})
    @reporting_errors
    async def add_private_note(
        conversation_id: Annotated[int, Field(ge=1, description="The conversation number.")],
        content: Annotated[
            str, Field(min_length=1, max_length=MAX_NOTE_CHARS, description="The note text. Only the team sees it.")
        ],
    ) -> dict[str, Any]:
        """Add an internal note to a chat. It is never sent to the customer."""
        if not content.strip():
            raise ToolError("The note is empty.")
        body = {"content": content, "message_type": "outgoing", "private": True}
        data = await ctx.client.post_account(f"/conversations/{conversation_id}/messages", body)
        message = shaping.compact_message(data if isinstance(data, dict) else {}, text_limit)
        if not message.get("private"):
            # Never report success for something that might have reached the customer.
            raise ToolError("Chatwoot did not mark the message private. Check the chat in Chatwoot right away.")
        return message

    async def add_labels(path: str, labels: list[str]) -> list[str]:
        """Add labels to a chat or a customer. Chatwoot replaces the whole set, so read it and send the union."""
        known = await ctx.client.get_account("/labels")
        rows = known.get("payload", []) if isinstance(known, dict) else []
        titles = {row.get("title") for row in rows if isinstance(row, dict)}
        unknown = sorted({label for label in labels if label not in titles})
        if unknown:
            raise ToolError(f"These labels do not exist: {', '.join(unknown)}. Use list_labels to see the real ones.")
        current = await ctx.client.get_account(path)
        have = current.get("payload", []) if isinstance(current, dict) else []
        merged = list(dict.fromkeys([*have, *labels]))
        data = await ctx.client.post_account(path, {"labels": merged})
        return data.get("payload", merged) if isinstance(data, dict) else merged

    @mcp.tool(annotations=SAFE_IDEMPOTENT_WRITE, tags={TIER_WRITE, "module:writes"})
    @reporting_errors
    async def add_conversation_labels(
        conversation_id: Annotated[int, Field(ge=1, description="The conversation number.")],
        labels: Labels,
    ) -> dict[str, Any]:
        """Add labels to a chat. Existing labels stay. Unknown labels are refused; see list_labels."""
        result = await add_labels(f"/conversations/{conversation_id}/labels", labels)
        return {"conversation_id": conversation_id, "labels": result}

    @mcp.tool(annotations=SAFE_IDEMPOTENT_WRITE, tags={TIER_WRITE, "module:writes"})
    @reporting_errors
    async def add_contact_labels(
        contact_id: Annotated[int, Field(ge=1, description="The customer's id.")],
        labels: Labels,
    ) -> dict[str, Any]:
        """Add labels to a customer. Existing labels stay. Unknown labels are refused; see list_labels."""
        result = await add_labels(f"/contacts/{contact_id}/labels", labels)
        return {"contact_id": contact_id, "labels": result}
