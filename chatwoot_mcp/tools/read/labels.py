"""Labels, read: the labels on a chat or a customer. Any agent or API client can attach free text as a label, so
every label is framed as untrusted (``shaping.shape_labels``). ``list_labels`` lists the account's label
definitions, which administrators write, as they are.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import rows
from chatwoot_mcp.shaping import shape_labels
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, ContactId, ConversationId, ToolContext

MODULE = "labels"


def current_labels(data: Any) -> list[str]:
    return [label for label in rows(data) if isinstance(label, str)]


def register(mcp: FastMCP, context: ToolContext) -> None:
    async def read(path: str, key: str, item_id: int) -> dict[str, Any] | str:
        try:
            session = await context.session()
            labels = current_labels(await session.get(path))
        except ChatwootError as error:
            return to_tool_error(error)
        return {
            key: item_id,
            "labels": shape_labels(labels, source=f"{key.removesuffix('_id')}_label", item_id=item_id),
        }

    @mcp.tool(**context.tool("Get conversation labels", READ, module=MODULE, tier=Tier.AGENT))
    async def get_conversation_labels(conversation_id: ConversationId) -> dict[str, Any] | str:
        """List the labels on one chat."""
        return await read(f"/conversations/{conversation_id}/labels", "conversation_id", conversation_id)

    @mcp.tool(**context.tool("Get contact labels", READ, module=MODULE, tier=Tier.AGENT))
    async def get_contact_labels(contact_id: ContactId) -> dict[str, Any] | str:
        """List the labels on one customer."""
        return await read(f"/contacts/{contact_id}/labels", "contact_id", contact_id)
