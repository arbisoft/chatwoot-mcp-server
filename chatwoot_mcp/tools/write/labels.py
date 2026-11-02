"""Labels, write: add labels to a chat or a customer, and take them off a chat.

``POST .../labels`` **replaces** the whole set, so every write reads the current labels first and sends the
result. Labels that do not exist in the account are refused instead of created, so no stray labels appear.
Chatwoot stores label titles in lower case, so input is lower-cased before matching.

Chatwoot offers no atomic add or remove, and its own UI replaces the set the same way, so two writers in the same
instant can still overwrite each other. A lock here could not cover the UI, other agents or automations.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Label
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import ADD, OVERWRITE, ContactId, ConversationId, ToolContext
from chatwoot_mcp.tools.read.labels import current_labels

MODULE = "labels"
MAX_LABELS_PER_CALL = 20

Labels = Annotated[list[str], Field(min_length=1, max_length=MAX_LABELS_PER_CALL, description="Existing label titles.")]


def register(mcp: FastMCP, context: ToolContext) -> None:
    async def add(path: str, key: str, item_id: int, labels: list[str]) -> dict[str, Any] | str:
        wanted = [label.lower() for label in labels]
        try:
            session = await context.session()
            catalogue, current = await asyncio.gather(session.get("/labels"), session.get(path))
            titles = {label.title for label in Label.parse_rows(catalogue)}
            unknown = sorted({label for label in wanted if label not in titles})
            if unknown:
                return f"Error: these labels do not exist: {', '.join(unknown)}; use list_labels to see the real ones"
            merged = list(dict.fromkeys([*current_labels(current), *wanted]))
            saved = current_labels(await session.post(path, {"labels": merged}))
        except ChatwootError as error:
            return to_tool_error(error)
        return {key: item_id, "labels": saved}

    @mcp.tool(**context.tool("Add conversation labels", ADD, module=MODULE, tier=Tier.AGENT))
    async def add_conversation_labels(conversation_id: ConversationId, labels: Labels) -> dict[str, Any] | str:
        """Add labels to a chat. Existing labels stay. Unknown labels are refused; see list_labels."""
        return await add(f"/conversations/{conversation_id}/labels", "conversation_id", conversation_id, labels)

    @mcp.tool(**context.tool("Add contact labels", ADD, module=MODULE, tier=Tier.AGENT))
    async def add_contact_labels(contact_id: ContactId, labels: Labels) -> dict[str, Any] | str:
        """Add labels to a customer. Existing labels stay. Unknown labels are refused; see list_labels."""
        return await add(f"/contacts/{contact_id}/labels", "contact_id", contact_id, labels)

    @mcp.tool(**context.tool("Remove conversation labels", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def remove_conversation_labels(conversation_id: ConversationId, labels: Labels) -> dict[str, Any] | str:
        """Take labels off a chat. Other labels stay. Labels the chat does not have are ignored."""
        path = f"/conversations/{conversation_id}/labels"
        drop = {label.lower() for label in labels}
        try:
            session = await context.session()
            kept = [label for label in current_labels(await session.get(path)) if label not in drop]
            saved = current_labels(await session.post(path, {"labels": kept}))
        except ChatwootError as error:
            return to_tool_error(error)
        return {"conversation_id": conversation_id, "labels": saved}
