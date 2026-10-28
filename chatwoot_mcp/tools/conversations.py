"""Reading chats: list, search, open one, read its messages, count them."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp import shaping
from chatwoot_mcp.tools.support import READ_ONLY, TIER_READ, ToolContext, page_info, reporting_errors

Status = Literal["all", "open", "resolved", "pending", "snoozed"]
Assignee = Literal["me", "unassigned", "all", "assigned"]
ConversationId = Annotated[int, Field(ge=1, description="The conversation number shown in Chatwoot.")]
InboxId = Annotated[int | None, Field(ge=1, description="Only chats in this inbox. Use list_inboxes to find ids.")]
TeamId = Annotated[int | None, Field(ge=1, description="Only chats assigned to this team. Use list_teams for ids.")]
Labels = Annotated[list[str] | None, Field(description="Only chats that carry all of these labels.")]
Page = Annotated[int, Field(ge=1, description="Page number, starting at 1.")]


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    settings = ctx.settings
    shown = settings.max_items
    text_limit = settings.max_text_chars

    def conversation_list(data: Any, page: int) -> dict[str, Any]:
        body = data.get("data", data) if isinstance(data, dict) else {}
        items, cut = shaping.limit_items(body.get("payload"), shown)
        return {
            **page_info(page, cut, shown),
            "counts": body.get("meta"),
            "conversations": [shaping.compact_conversation(c, text_limit) for c in items],
        }

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:conversations"})
    @reporting_errors
    async def list_conversations(
        status: Annotated[Status, Field(description="Which chats to list.")] = "open",
        assignee_type: Annotated[
            Assignee, Field(description="Whose chats: yours, unassigned, assigned or all.")
        ] = "all",
        inbox_id: InboxId = None,
        team_id: TeamId = None,
        labels: Labels = None,
        page: Page = 1,
    ) -> dict[str, Any]:
        """List support chats, newest activity first, with who the customer is and the last message."""
        params = {
            "status": status,
            "assignee_type": assignee_type,
            "inbox_id": inbox_id,
            "team_id": team_id,
            "labels": labels,
            "page": page,
        }
        return conversation_list(await ctx.client.get_account("/conversations", params=params), page)

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:conversations"})
    @reporting_errors
    async def search_conversations(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Words to find in the messages.")],
        status: Annotated[Status, Field(description="Which chats to search.")] = "all",
        inbox_id: InboxId = None,
        page: Page = 1,
    ) -> dict[str, Any]:
        """Find chats whose messages contain the given words."""
        params = {"q": query, "status": status, "inbox_id": inbox_id, "page": page}
        return conversation_list(await ctx.client.get_account("/conversations", params=params), page)

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:conversations"})
    @reporting_errors
    async def get_conversation(conversation_id: ConversationId) -> dict[str, Any]:
        """Open one chat: its status, customer, assignee, team, labels and last message."""
        data = await ctx.client.get_account(f"/conversations/{conversation_id}")
        return shaping.compact_conversation(data, text_limit)

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:conversations"})
    @reporting_errors
    async def get_messages(
        conversation_id: ConversationId,
        before: Annotated[
            int | None, Field(ge=1, description="Only messages older than this message id (to read further back).")
        ] = None,
        after: Annotated[int | None, Field(ge=1, description="Only messages newer than this message id.")] = None,
    ) -> dict[str, Any]:
        """Read the messages in a chat, oldest first. Customer text is wrapped in <untrusted_content>."""
        params = {"before": before, "after": after}
        data = await ctx.client.get_account(f"/conversations/{conversation_id}/messages", params=params)
        items, cut = shaping.limit_items(data.get("payload") if isinstance(data, dict) else None, shown)
        meta = data.get("meta", {}) if isinstance(data, dict) else {}
        out: dict[str, Any] = {
            "conversation_id": conversation_id,
            "labels": meta.get("labels") or [],
            "messages": [shaping.compact_message(m, text_limit) for m in items],
        }
        if cut:
            last_id = items[-1].get("id")
            out["truncated"] = f"Only the first {shown} messages are shown. Call again with after={last_id}."
        return out

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:conversations"})
    @reporting_errors
    async def get_conversation_counts(
        status: Annotated[Status, Field(description="Which chats to count.")] = "open",
        inbox_id: InboxId = None,
        team_id: TeamId = None,
        labels: Labels = None,
    ) -> dict[str, Any]:
        """Count chats: yours, assigned, unassigned and all."""
        params = {"status": status, "inbox_id": inbox_id, "team_id": team_id, "labels": labels}
        data = await ctx.client.get_account("/conversations/meta", params=params)
        return (data.get("meta") if isinstance(data, dict) else None) or {}

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:conversations"})
    @reporting_errors
    async def get_conversation_labels(conversation_id: ConversationId) -> dict[str, Any]:
        """List the labels on one chat."""
        data = await ctx.client.get_account(f"/conversations/{conversation_id}/labels")
        return {
            "conversation_id": conversation_id,
            "labels": (data.get("payload") if isinstance(data, dict) else None) or [],
        }
