"""Conversations (chats): list, search, filter, open one, count, attachments; and, unless read-only, assign, status,
priority and custom fields.

These writes run at once, like Zammad's ``update_ticket``, except resolving: Chatwoot then sends the customer a
satisfaction survey when the inbox has one, so it is ``prepare_resolve_conversation`` then ``resolve_conversation``.
Replies and new conversations are in ``replies``, behind the same confirm step.

Chatwoot facts the writes rely on:

- ``toggle_status`` reads ``snoozed_until`` with ``strptime('%s')``: Unix seconds. An ISO string parses as a moment
  in 1970 and the chat wakes at once.
- Opening a chat assigns it to the key's owner when their role is agent (not administrator).
- Changing the team drops an assignee who is not in it (and may auto-assign a member), so the team is set first.
- ``POST .../custom_attributes`` replaces the whole set unless ``merge`` is sent, which older versions ignore, so
  the current fields are sent along with the new ones.
- The ``labels`` filter matches *any* of the labels.
- A search (``q``) skips the status filter, so ``search_conversations`` applies the status itself.
- Attachments come 100 per Chatwoot page; the tool splits each into pages of ``ATTACHMENTS_PER_PAGE``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Attachment, Conversation, CustomAttributeDefinition, Inbox, payload
from chatwoot_mcp.shaping import compact, shape_attachment, shape_attributes, shape_conversation, shape_person
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import (
    OVERWRITE,
    PREPARE,
    READ,
    ChatwootSession,
    ConversationId,
    Page,
    ToolContext,
    as_dict,
    confirmation_refusal,
    limited,
    page_info,
    prepared,
)

MODULE = "conversations"
RESOLVE = "resolve_conversation"
ATTACHMENTS_PER_PAGE = 25
CHATWOOT_ATTACHMENTS_PER_PAGE = 100

Status = Literal["all", "open", "resolved", "pending", "snoozed"]
NewStatus = Literal["open", "pending", "snoozed"]
Priority = Literal["none", "low", "medium", "high", "urgent"]
Assignee = Literal["me", "unassigned", "all", "assigned"]
InboxId = Annotated[int | None, Field(ge=1, description="Only chats in this inbox. Use list_inboxes to find ids.")]
TeamId = Annotated[int | None, Field(ge=1, description="Only chats assigned to this team. Use list_teams for ids.")]
Labels = Annotated[list[str] | None, Field(description="Only chats that carry at least one of these labels.")]
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


def snooze_seconds(status: str, snoozed_until: str | None) -> int | None:
    """Check the wake time and return it as Unix seconds; raise ``ValueError`` with a plain reason."""
    if status != "snoozed":
        if snoozed_until is not None:
            raise ValueError("`snoozed_until` can be used only with status snoozed")
        return None
    if snoozed_until is None:
        raise ValueError("snoozing needs `snoozed_until`, like 2026-11-01T09:00:00+00:00")
    try:
        moment = datetime.fromisoformat(snoozed_until)
    except ValueError:
        moment = None
    if moment is None or moment.tzinfo is None:
        raise ValueError("`snoozed_until` must be a date and time with a time zone, like 2026-11-01T09:00:00+00:00")
    if moment <= datetime.now(UTC):
        raise ValueError("`snoozed_until` must be in the future")
    return int(moment.timestamp())


def register(mcp: FastMCP, context: ToolContext) -> None:
    def conversation_list(data: Any, page: int, *, status: str = "all") -> dict[str, Any]:
        body = as_dict(as_dict(data).get("data", data))
        rows = [c for c in Conversation.parse_rows(body) if status == "all" or c.status == status]
        items, cut = limited(rows, context.max_items)
        return {
            **page_info(page, cut, context.max_items),
            "counts": body.get("meta"),
            "conversations": [shape_conversation(c, context.links, max_chars=context.max_chars) for c in items],
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
        {"field": "last_activity_at", "operator": "days_before", "values": [3]};
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
        return as_dict(data.get("meta"))

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

    if not context.writes_enabled:
        return

    @mcp.tool(**context.tool("Assign conversation", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def assign_conversation(
        conversation_id: ConversationId,
        agent_id: Annotated[int | None, Field(ge=1, description="The agent to assign. See list_agents.")] = None,
        team_id: Annotated[int | None, Field(ge=1, description="The team to assign. See list_teams.")] = None,
    ) -> dict[str, Any] | str:
        """Assign a chat to an agent, a team, or both, replacing who it is assigned to now.

        Setting only a team removes the current agent if they are not in it, and Chatwoot may auto-assign a member.
        """
        if agent_id is None and team_id is None:
            return "Error: give agent_id or team_id (or both)"
        path = f"/conversations/{conversation_id}/assignments"
        try:
            session = await context.session()
            # Team first: changing the team drops an assignee who is not in it, which would undo an agent set before.
            if team_id is not None:
                await session.post(path, {"team_id": team_id})
        except ChatwootError as error:
            return to_tool_error(error)
        if agent_id is not None:
            try:
                await session.post(path, {"assignee_id": agent_id})
            except ChatwootError as error:
                prefix = "the team was assigned, but assigning the agent failed: " if team_id is not None else ""
                return f"Error: {prefix}{to_tool_error(error).removeprefix('Error: ')}"
        return {"conversation_id": conversation_id, "assigned_agent_id": agent_id, "assigned_team_id": team_id}

    @mcp.tool(**context.tool("Set conversation status", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def set_conversation_status(
        conversation_id: ConversationId,
        status: Annotated[NewStatus, Field(description="The new status.")],
        snoozed_until: Annotated[
            str | None, Field(description="Only for snoozed: when it wakes, as 2026-11-01T09:00:00+00:00.")
        ] = None,
    ) -> dict[str, Any] | str:
        """Change a chat's status: open, pending or snoozed. To resolve a chat use prepare_resolve_conversation.

        Opening a chat also assigns it to the key's owner when their role is agent (Chatwoot's rule).
        """
        try:
            wake_at = snooze_seconds(status, snoozed_until)
        except ValueError as error:
            return f"Error: {error}"
        body: dict[str, Any] = {"status": status}
        if wake_at is not None:
            body["snoozed_until"] = wake_at
        try:
            session = await context.session()
            done = payload(await session.post(f"/conversations/{conversation_id}/toggle_status", body))
        except ChatwootError as error:
            return to_tool_error(error)
        return {
            "conversation_id": conversation_id,
            "status": done.get("current_status", status),
            "snoozed_until": done.get("snoozed_until"),
        }

    @mcp.tool(**context.tool("Prepare to resolve conversation", PREPARE, module=MODULE, tier=Tier.AGENT))
    async def prepare_resolve_conversation(conversation_id: ConversationId) -> dict[str, Any] | str:
        """Preview resolving a chat and get a confirmation token. Nothing changes yet.

        Resolving sends the customer a satisfaction survey when the inbox has surveys on; the preview says so. Show
        it to the user, and only once they approve, call resolve_conversation with the token and the same id.
        """
        try:
            session = await context.session()
            chat = Conversation.parse(await session.get(f"/conversations/{conversation_id}"))
            if chat.status == "resolved":
                return f"Error: conversation {conversation_id} is already resolved"
            inbox = Inbox.parse(await session.get(f"/inboxes/{chat.inbox_id}")) if chat.inbox_id else None
        except ChatwootError as error:
            return to_tool_error(error)
        survey = bool(inbox and inbox.csat_survey_enabled)
        preview = compact(
            {
                "conversation_id": conversation_id,
                "from": chat.status,
                "to": "resolved",
                "customer": shape_person(chat.meta.sender, source="contact"),
                "inbox": inbox.name if inbox else None,
                "warning": "The customer will be sent a satisfaction survey." if survey else None,
                "url": context.links.conversation(conversation_id),
            }
        )
        return await prepared(
            context, session, action=RESOLVE, payload={"conversation_id": conversation_id}, preview=preview
        )

    @mcp.tool(**context.tool("Resolve conversation", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def resolve_conversation(
        confirmation_token: Annotated[str, Field(min_length=1, max_length=200, description="From the prepare tool.")],
        conversation_id: ConversationId,
    ) -> dict[str, Any] | str:
        """Resolve the chat the user approved, from prepare_resolve_conversation's preview. The token works once."""
        try:
            session = await context.session()
        except ChatwootError as error:
            return to_tool_error(error)
        refusal = await confirmation_refusal(
            context, session, confirmation_token, action=RESOLVE, payload={"conversation_id": conversation_id}
        )
        if refusal:
            return refusal
        try:
            path = f"/conversations/{conversation_id}/toggle_status"
            done = payload(await session.post(path, {"status": "resolved"}))
        except ChatwootError as error:
            return to_tool_error(error)
        return {"conversation_id": conversation_id, "status": done.get("current_status", "resolved")}

    @mcp.tool(**context.tool("Set conversation priority", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def set_conversation_priority(
        conversation_id: ConversationId,
        priority: Annotated[Priority, Field(description="The new priority. `none` clears it.")],
    ) -> dict[str, Any] | str:
        """Set a chat's priority: none, low, medium, high or urgent."""
        try:
            session = await context.session()
            await session.post(
                f"/conversations/{conversation_id}/toggle_priority",
                {"priority": None if priority == "none" else priority},
            )
        except ChatwootError as error:
            return to_tool_error(error)
        return {"conversation_id": conversation_id, "priority": priority}

    async def known_attribute_keys(session: ChatwootSession) -> set[str]:
        params = {"attribute_model": "conversation_attribute"}
        return {
            d.attribute_key
            for d in CustomAttributeDefinition.parse_rows(await session.get("/custom_attribute_definitions", params))
        }

    @mcp.tool(**context.tool("Set conversation custom attributes", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def set_conversation_custom_attributes(
        conversation_id: ConversationId,
        attributes: Annotated[
            dict[str, str | int | float | bool],
            Field(
                min_length=1,
                max_length=20,
                description="Field keys and new values. Keys from list_custom_attribute_definitions.",
            ),
        ],
    ) -> dict[str, Any] | str:
        """Fill in a chat's custom fields, such as an order number. Fields not given stay as they are."""
        try:
            session = await context.session()
            known, chat = await asyncio.gather(
                known_attribute_keys(session), session.get(f"/conversations/{conversation_id}")
            )
            unknown = sorted(set(attributes) - known)
            if unknown:
                return f"Error: these fields do not exist: {', '.join(unknown)}; use list_custom_attribute_definitions"
            current = Conversation.parse(chat).custom_attributes
            body = {"custom_attributes": {**current, **attributes}}
            saved = as_dict(await session.post(f"/conversations/{conversation_id}/custom_attributes", body))
        except ChatwootError as error:
            return to_tool_error(error)
        return {
            "conversation_id": conversation_id,
            "custom_attributes": shape_attributes(
                as_dict(saved.get("custom_attributes")), source="conversation_attribute", item_id=conversation_id
            ),
        }
