"""Reading chats: list, search, filter, open one, read its messages and attachments, count them."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from chatwoot_mcp import shaping
from chatwoot_mcp.tools.support import READ_ONLY, TIER_READ, ToolContext, page_info, reporting_errors

Status = Literal["all", "open", "resolved", "pending", "snoozed"]
Assignee = Literal["me", "unassigned", "all", "assigned"]
ConversationId = Annotated[int, Field(ge=1, description="The conversation number shown in Chatwoot.")]
InboxId = Annotated[int | None, Field(ge=1, description="Only chats in this inbox. Use list_inboxes to find ids.")]
TeamId = Annotated[int | None, Field(ge=1, description="Only chats assigned to this team. Use list_teams for ids.")]
Labels = Annotated[list[str] | None, Field(description="Only chats that carry at least one of these labels.")]
Page = Annotated[int, Field(ge=1, description="Page number, starting at 1.")]
FilterOperator = Literal[
    "equal_to",
    "not_equal_to",
    "contains",
    "does_not_contain",
    "is_present",
    "is_not_present",
    "is_greater_than",
    "is_less_than",
    "days_before",
]


class Condition(BaseModel):
    field: str = Field(
        min_length=1,
        max_length=100,
        description=(
            "status, priority, assignee_id, team_id, inbox_id, contact_id, labels, created_at, last_activity_at, "
            "mail_subject, or a custom attribute key from list_custom_attribute_definitions."
        ),
    )
    operator: FilterOperator
    values: list[str | int] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "What to compare with. Dates as 2026-10-01; days_before takes a number of days (1 to 998); "
            "is_present and is_not_present take none."
        ),
    )


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
    async def filter_conversations(
        conditions: Annotated[list[Condition], Field(min_length=1, max_length=10, description="The conditions.")],
        match: Annotated[Literal["all", "any"], Field(description="Chats must meet all conditions, or any.")] = "all",
        page: Page = 1,
    ) -> dict[str, Any]:
        """Find chats by detailed conditions, for example high priority and unassigned, or no activity for 3 days.

        Examples: {"field": "assignee_id", "operator": "is_not_present"};
        {"field": "last_activity_at", "operator": "days_before", "values": [3]};
        {"field": "priority", "operator": "equal_to", "values": ["high"]}.
        """
        join = "and" if match == "all" else "or"
        payload = [
            {"attribute_key": c.field, "filter_operator": c.operator, "values": c.values, "query_operator": join}
            for c in conditions
        ]
        payload[-1]["query_operator"] = None  # Chatwoot refuses a joining word after the last condition
        data = await ctx.client.post_account("/conversations/filter", {"payload": payload, "page": page})
        return conversation_list(data, page)

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
        """Read a chat's messages, oldest first. With no ids, the latest 20; pass `before` to read further back.

        Customer text is wrapped in <untrusted_content>.
        """
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
    async def list_conversation_attachments(conversation_id: ConversationId, page: Page = 1) -> dict[str, Any]:
        """List the files sent in a chat (type, size, who sent it), newest first. Files are not opened."""
        data = await ctx.client.get_account(f"/conversations/{conversation_id}/attachments", params={"page": page})
        items, cut = shaping.limit_items(shaping.rows(data), shown)
        return {
            "conversation_id": conversation_id,
            **page_info(page, cut, shown),
            "total_count": (data.get("meta") or {}).get("total_count") if isinstance(data, dict) else None,
            "attachments": [
                {
                    **{key: a.get(key) for key in ("id", "message_id", "file_type", "extension", "file_size")},
                    "created_at": a.get("created_at"),
                    "sender": shaping.person(a.get("sender"), "id", "name", "type"),
                }
                for a in items
                if isinstance(a, dict)
            ],
        }

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:conversations"})
    @reporting_errors
    async def get_conversation_labels(conversation_id: ConversationId) -> dict[str, Any]:
        """List the labels on one chat."""
        data = await ctx.client.get_account(f"/conversations/{conversation_id}/labels")
        return {
            "conversation_id": conversation_id,
            "labels": (data.get("payload") if isinstance(data, dict) else None) or [],
        }
