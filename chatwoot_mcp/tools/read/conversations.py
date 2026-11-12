"""Conversations (chats), read: list, search, filter, open one, count, and list attachments.

- The ``labels`` filter matches *any* of the labels.
- A search (``q``) skips the status filter, so ``search_conversations`` applies the status itself.
- Chatwoot pages chats itself (``CONVERSATION_RESULTS_PER_PAGE``, 25 by default), so each Chatwoot page is returned
  whole: cutting it would leave the rest of the page unreachable.
- Attachments come 100 per Chatwoot page; the tool splits each into pages of ``ATTACHMENTS_PER_PAGE``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Attachment, Conversation
from chatwoot_mcp.shaping import shape_attachment, shape_conversation, shape_report
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import (
    MAX_ID,
    READ,
    ConversationId,
    Page,
    ToolContext,
    as_dict,
)

MODULE = "conversations"
ATTACHMENTS_PER_PAGE = 25
CHATWOOT_ATTACHMENTS_PER_PAGE = 100

Status = Literal["all", "open", "resolved", "pending", "snoozed"]
Assignee = Literal["me", "unassigned", "all", "assigned"]
InboxId = Annotated[
    int | None, Field(ge=1, le=MAX_ID, description="Only chats in this inbox. Use list_inboxes to find ids.")
]
TeamId = Annotated[
    int | None, Field(ge=1, le=MAX_ID, description="Only chats assigned to this team. Use list_teams for ids.")
]
Labels = Annotated[
    list[str] | None, Field(max_length=20, description="Only chats that carry at least one of these labels.")
]
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
    values: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            'What to compare with, as text: ids, numbers and true/false too ("12", "true"). Dates as '
            "2026-10-01; days_before takes a number of days (1 to 998); is_present and is_not_present take none."
        ),
    )


def register(mcp: FastMCP, context: ToolContext) -> None:
    def conversation_list(data: Any, page: int, *, status: str = "all") -> dict[str, Any]:
        body = as_dict(as_dict(data).get("data", data))
        rows = [c for c in Conversation.parse_rows(body) if status == "all" or c.status == status]
        return {
            "page": page,
            "counts": shape_report(body.get("meta")),
            "conversations": [shape_conversation(c, context.links, max_chars=context.max_chars) for c in rows],
        }

    @mcp.tool(**context.tool("List conversations", READ, module=MODULE, tier=Tier.AGENT))
    async def list_conversations(
        status: Annotated[Status, Field(description="Which chats to list.")] = "open",
        assignee_type: Annotated[
            Assignee, Field(description="Whose chats: yours, unassigned, assigned or all.")
        ] = "all",
        inbox_id: InboxId = None,
        team_id: TeamId = None,
        labels: Labels = None,
        page: Page = 1,
    ) -> dict[str, Any] | str:
        """List support chats, newest activity first, with who the customer is and the last message."""
        params = {
            "status": status,
            "assignee_type": assignee_type,
            "inbox_id": inbox_id,
            "team_id": team_id,
            "labels": labels,
            "page": page,
        }
        try:
            session = await context.session()
            return conversation_list(await session.get("/conversations", params), page)
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("Search conversations", READ, module=MODULE, tier=Tier.AGENT))
    async def search_conversations(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Words to find in the messages.")],
        status: Annotated[Status, Field(description="Which chats to keep from each page of matches.")] = "all",
        inbox_id: InboxId = None,
        page: Page = 1,
    ) -> dict[str, Any] | str:
        """Find chats whose messages contain the given words.

        Chatwoot searches every status, so a status other than all is applied to each page of matches: a page can
        hold fewer chats, or none, while later pages still have some.
        """
        params = {"q": query, "inbox_id": inbox_id, "page": page}
        try:
            session = await context.session()
            return conversation_list(await session.get("/conversations", params), page, status=status)
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("Filter conversations", READ, module=MODULE, tier=Tier.AGENT))
    async def filter_conversations(
        conditions: Annotated[list[Condition], Field(min_length=1, max_length=10, description="The conditions.")],
        match: Annotated[Literal["all", "any"], Field(description="Chats must meet all conditions, or any.")] = "all",
        page: Page = 1,
    ) -> dict[str, Any] | str:
        """Find chats by detailed conditions, for example high priority and unassigned, or no activity for 3 days.

        Examples: {"field": "assignee_id", "operator": "is_not_present"};
        {"field": "last_activity_at", "operator": "days_before", "values": ["3"]};
        {"field": "priority", "operator": "equal_to", "values": ["high"]}.
        """
        join = "and" if match == "all" else "or"
        query_payload = [
            {"attribute_key": c.field, "filter_operator": c.operator, "values": c.values, "query_operator": join}
            for c in conditions
        ]
        query_payload[-1]["query_operator"] = None  # Chatwoot refuses a joining word after the last condition
        try:
            session = await context.session()
            data = await session.search("/conversations/filter", {"payload": query_payload, "page": page})
            return conversation_list(data, page)
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("Get conversation", READ, module=MODULE, tier=Tier.AGENT))
    async def get_conversation(conversation_id: ConversationId) -> dict[str, Any] | str:
        """Open one chat: status, customer, assignee, team, labels, custom fields and last message."""
        try:
            session = await context.session()
            conversation = Conversation.parse(await session.get(f"/conversations/{conversation_id}"))
        except ChatwootError as error:
            return to_tool_error(error)
        return shape_conversation(conversation, context.links, max_chars=context.max_chars)

    @mcp.tool(**context.tool("Get conversation counts", READ, module=MODULE, tier=Tier.AGENT))
    async def get_conversation_counts(
        status: Annotated[Status, Field(description="Which chats to count.")] = "open",
        inbox_id: InboxId = None,
        team_id: TeamId = None,
        labels: Labels = None,
    ) -> dict[str, Any] | str:
        """Count chats: yours, assigned, unassigned and all."""
        params = {"status": status, "inbox_id": inbox_id, "team_id": team_id, "labels": labels}
        try:
            session = await context.session()
            data = as_dict(await session.get("/conversations/meta", params))
        except ChatwootError as error:
            return to_tool_error(error)
        return shape_report(as_dict(data.get("meta")))

    @mcp.tool(**context.tool("List conversation attachments", READ, module=MODULE, tier=Tier.AGENT))
    async def list_conversation_attachments(conversation_id: ConversationId, page: Page = 1) -> dict[str, Any] | str:
        """List the files sent in a chat (type, size, who sent it), newest first, 25 per page. Files are not opened."""
        per_chatwoot_page = CHATWOOT_ATTACHMENTS_PER_PAGE // ATTACHMENTS_PER_PAGE
        chatwoot_page, part = divmod(page - 1, per_chatwoot_page)
        try:
            session = await context.session()
            data = await session.get(f"/conversations/{conversation_id}/attachments", {"page": chatwoot_page + 1})
            rows = Attachment.parse_rows(data)
        except ChatwootError as error:
            return to_tool_error(error)
        total = as_dict(as_dict(data).get("meta")).get("total_count")
        start = part * ATTACHMENTS_PER_PAGE
        out: dict[str, Any] = {"conversation_id": conversation_id, "page": page, "total_count": total}
        if isinstance(total, int) and total > page * ATTACHMENTS_PER_PAGE:
            out["next_page"] = page + 1
        return {**out, "attachments": [shape_attachment(a) for a in rows[start : start + ATTACHMENTS_PER_PAGE]]}
