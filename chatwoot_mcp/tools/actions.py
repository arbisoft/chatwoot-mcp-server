"""Actions that change things for customers or colleagues. Only registered when CHATWOOT_MCP_ENABLE_ACTIONS=true.

Every action has two steps. The first call changes nothing: it looks up what the action would touch and returns
a plain preview and a one-time `confirm_token`. The assistant must show the preview to the person, and only
after they clearly agree call the tool again with the same arguments and the token. See confirmations.py.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from chatwoot_mcp import shaping
from chatwoot_mcp.client import ChatwootError
from chatwoot_mcp.tools.support import (
    ACTION_ADDS,
    ACTION_OVERWRITES,
    TIER_ACTION,
    ToolContext,
    reporting_errors,
)

MAX_MESSAGE_CHARS = 5000
SENT_TO_CUSTOMER = "This message will be sent to the customer and cannot be unsent."
NEXT_STEP = (
    "Show this preview to the user. Only after they clearly agree, call this tool again with the same arguments "
    "and this confirm_token. Do not call it again without their agreement."
)
PHONE = re.compile(r"\+[1-9][0-9]{6,14}")

ConversationId = Annotated[int, Field(ge=1, description="The conversation number.")]
ContactId = Annotated[int, Field(ge=1, description="The customer's id.")]
Message = Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS, description="The message text.")]
ConfirmToken = Annotated[
    str | None,
    Field(description="Leave empty the first time. After the user agrees to the preview, pass its confirm_token."),
]
Email = Annotated[str | None, Field(max_length=254, description="The customer's email address.")]
Phone = Annotated[str | None, Field(description="International format with no spaces, like +15550100.")]
Identifier = Annotated[str | None, Field(max_length=255, description="Your own id for this customer.")]
Status = Literal["open", "pending", "snoozed", "resolved"]
Priority = Literal["none", "low", "medium", "high", "urgent"]


def _person(raw: Any, *fields: str) -> dict[str, Any] | None:
    return {field: raw.get(field) for field in fields} if isinstance(raw, dict) and raw else None


def _rows(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    return data.get("payload", []) if isinstance(data, dict) and isinstance(data.get("payload"), list) else []


def _payload(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        inner = data.get("payload", data)
        return inner if isinstance(inner, dict) else {}
    return {}


def _no_blank(value: str | None, name: str) -> None:
    if value is not None and not value.strip():
        raise ToolError(f"{name} is empty.")


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    text_limit = ctx.settings.max_text_chars

    async def gated(
        tool: str,
        arguments: dict[str, Any],
        token: str | None,
        build_preview: Callable[[], Awaitable[dict[str, Any]]],
        execute: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        owner = ctx.owner()
        if owner is None:
            raise ChatwootError("No Chatwoot key was provided for this request.")
        if token is None:
            preview = await build_preview()
            return {
                "status": "needs_confirmation",
                "action": tool,
                "preview": preview,
                "confirm_token": ctx.confirmations.issue(owner, tool, arguments),
                "expires_in_seconds": ctx.confirmations.ttl_seconds,
                "next_step": NEXT_STEP,
            }
        if not ctx.confirmations.redeem(owner, tool, arguments, token):
            raise ToolError(
                "The confirmation is missing, expired, already used, or the request was changed. "
                "Call the tool again without confirm_token to get a new preview."
            )
        return await execute()

    async def conversation(conversation_id: int) -> dict[str, Any]:
        data = await ctx.client.get_account(f"/conversations/{conversation_id}")
        return data if isinstance(data, dict) else {}

    def chat_summary(raw: dict[str, Any]) -> dict[str, Any]:
        meta = raw.get("meta") or {}
        return {
            "conversation_id": raw.get("id"),
            "status": raw.get("status"),
            "channel": meta.get("channel"),
            "contact": _person(meta.get("sender"), "name", "email"),
        }

    # --- send_reply ----------------------------------------------------------------------------------------------

    @mcp.tool(annotations=ACTION_ADDS, tags={TIER_ACTION, "module:actions"})
    @reporting_errors
    async def send_reply(
        conversation_id: ConversationId, content: Message, confirm_token: ConfirmToken = None
    ) -> dict[str, Any]:
        """Send a reply to the customer in a chat. It reaches the customer, so it needs the user's agreement."""
        _no_blank(content, "The message")

        async def build_preview() -> dict[str, Any]:
            chat = await conversation(conversation_id)
            if chat.get("can_reply") is False:
                raise ToolError(
                    "This chat cannot be replied to right now (for example the channel's reply window has closed)."
                )
            meta = chat.get("meta") or {}
            return {
                "conversation_id": conversation_id,
                "to": _person(meta.get("sender"), "name", "email"),
                "channel": meta.get("channel"),
                "message": content,
                "warning": SENT_TO_CUSTOMER,
            }

        async def execute() -> dict[str, Any]:
            body = {"content": content, "message_type": "outgoing", "private": False}
            data = await ctx.client.post_account(f"/conversations/{conversation_id}/messages", body)
            message = shaping.compact_message(data if isinstance(data, dict) else {}, text_limit)
            if message.get("private"):
                raise ToolError("Chatwoot marked the message private, so the customer did not get it. Check the chat.")
            return message

        arguments = {"conversation_id": conversation_id, "content": content}
        return await gated("send_reply", arguments, confirm_token, build_preview, execute)

    # --- assign_conversation -------------------------------------------------------------------------------------

    @mcp.tool(annotations=ACTION_OVERWRITES, tags={TIER_ACTION, "module:actions"})
    @reporting_errors
    async def assign_conversation(
        conversation_id: ConversationId,
        agent_id: Annotated[int | None, Field(ge=1, description="The agent to assign. See list_agents.")] = None,
        team_id: Annotated[int | None, Field(ge=1, description="The team to assign. See list_teams.")] = None,
        confirm_token: ConfirmToken = None,
    ) -> dict[str, Any]:
        """Assign a chat to an agent, a team, or both. This replaces who it is assigned to now."""
        if agent_id is None and team_id is None:
            raise ToolError("Give agent_id or team_id (or both).")

        async def build_preview() -> dict[str, Any]:
            chat = await conversation(conversation_id)
            meta = chat.get("meta") or {}
            agent = team = None
            if agent_id is not None:
                agent = next(
                    (a for a in _rows(await ctx.client.get_account("/agents")) if a.get("id") == agent_id), None
                )
                if agent is None:
                    raise ToolError(f"No agent with id {agent_id}. Use list_agents.")
            if team_id is not None:
                team = next((t for t in _rows(await ctx.client.get_account("/teams")) if t.get("id") == team_id), None)
                if team is None:
                    raise ToolError(f"No team with id {team_id}. Use list_teams.")
            return {
                **chat_summary(chat),
                "assign_to_agent": _person(agent, "id", "name", "email"),
                "assign_to_team": _person(team, "id", "name"),
                "currently": {
                    "agent": _person(meta.get("assignee"), "id", "name", "email"),
                    "team": _person(meta.get("team"), "id", "name"),
                },
            }

        async def execute() -> dict[str, Any]:
            path = f"/conversations/{conversation_id}/assignments"
            if agent_id is not None:
                await ctx.client.post_account(path, {"assignee_id": agent_id})
            if team_id is not None:
                try:
                    await ctx.client.post_account(path, {"team_id": team_id})
                except ChatwootError as error:
                    if agent_id is None:
                        raise
                    raise ToolError(f"The agent was assigned, but assigning the team failed: {error}") from None
            return {"conversation_id": conversation_id, "assigned_agent_id": agent_id, "assigned_team_id": team_id}

        arguments = {"conversation_id": conversation_id, "agent_id": agent_id, "team_id": team_id}
        return await gated("assign_conversation", arguments, confirm_token, build_preview, execute)

    # --- set_conversation_status ---------------------------------------------------------------------------------

    def snooze_time(status: str, snoozed_until: str | None) -> None:
        if status != "snoozed":
            if snoozed_until is not None:
                raise ToolError("`snoozed_until` can be used only with status snoozed.")
            return
        if snoozed_until is None:
            raise ToolError("Snoozing needs `snoozed_until`, like 2026-11-01T09:00:00+00:00.")
        try:
            moment = datetime.fromisoformat(snoozed_until)
        except ValueError:
            moment = None
        if moment is None or moment.tzinfo is None:
            raise ToolError("`snoozed_until` must be a date and time with a time zone, like 2026-11-01T09:00:00+00:00.")
        if moment <= datetime.now(UTC):
            raise ToolError("`snoozed_until` must be in the future.")

    @mcp.tool(annotations=ACTION_OVERWRITES, tags={TIER_ACTION, "module:actions"})
    @reporting_errors
    async def set_conversation_status(
        conversation_id: ConversationId,
        status: Annotated[Status, Field(description="The new status.")],
        snoozed_until: Annotated[
            str | None, Field(description="Only for snoozed: when it wakes, as 2026-11-01T09:00:00+00:00.")
        ] = None,
        confirm_token: ConfirmToken = None,
    ) -> dict[str, Any]:
        """Change a chat's status: open, pending, snoozed or resolved."""
        snooze_time(status, snoozed_until)

        async def build_preview() -> dict[str, Any]:
            chat = await conversation(conversation_id)
            preview = {**chat_summary(chat), "from": chat.get("status"), "to": status}
            if snoozed_until:
                preview["until"] = snoozed_until
            if status == "open":
                preview["side_effect"] = (
                    "Chatwoot also assigns the chat to the person who owns this key, if they are an agent, "
                    "replacing the current assignee."
                )
            return preview

        async def execute() -> dict[str, Any]:
            body: dict[str, Any] = {"status": status}
            if snoozed_until:
                body["snoozed_until"] = snoozed_until
            data = await ctx.client.post_account(f"/conversations/{conversation_id}/toggle_status", body)
            payload = _payload(data)
            return {
                "conversation_id": payload.get("conversation_id", conversation_id),
                "status": payload.get("current_status", status),
                "snoozed_until": payload.get("snoozed_until"),
            }

        arguments = {"conversation_id": conversation_id, "status": status, "snoozed_until": snoozed_until}
        return await gated("set_conversation_status", arguments, confirm_token, build_preview, execute)

    # --- set_conversation_priority -------------------------------------------------------------------------------

    @mcp.tool(annotations=ACTION_OVERWRITES, tags={TIER_ACTION, "module:actions"})
    @reporting_errors
    async def set_conversation_priority(
        conversation_id: ConversationId,
        priority: Annotated[Priority, Field(description="The new priority. `none` clears it.")],
        confirm_token: ConfirmToken = None,
    ) -> dict[str, Any]:
        """Set a chat's priority: none, low, medium, high or urgent."""

        async def build_preview() -> dict[str, Any]:
            chat = await conversation(conversation_id)
            return {**chat_summary(chat), "from": chat.get("priority") or "none", "to": priority}

        async def execute() -> dict[str, Any]:
            body = {"priority": None if priority == "none" else priority}
            await ctx.client.post_account(f"/conversations/{conversation_id}/toggle_priority", body)
            return {"conversation_id": conversation_id, "priority": priority}

        arguments = {"conversation_id": conversation_id, "priority": priority}
        return await gated("set_conversation_priority", arguments, confirm_token, build_preview, execute)

    # --- create_conversation -------------------------------------------------------------------------------------

    @mcp.tool(annotations=ACTION_ADDS, tags={TIER_ACTION, "module:actions"})
    @reporting_errors
    async def create_conversation(
        contact_id: ContactId,
        inbox_id: Annotated[int, Field(ge=1, description="The inbox to start the chat in. See list_inboxes.")],
        message: Annotated[
            str | None,
            Field(min_length=1, max_length=MAX_MESSAGE_CHARS, description="An optional first message to the customer."),
        ] = None,
        confirm_token: ConfirmToken = None,
    ) -> dict[str, Any]:
        """Start a new chat with a customer in an inbox, optionally with a first message that the customer receives."""
        _no_blank(message, "The message")

        async def build_preview() -> dict[str, Any]:
            contact = _payload(await ctx.client.get_account(f"/contacts/{contact_id}"))
            inbox = await ctx.client.get_account(f"/inboxes/{inbox_id}")
            preview: dict[str, Any] = {
                "contact": _person(contact, "id", "name", "email"),
                "inbox": _person(inbox, "id", "name", "channel_type"),
                "first_message": message,
            }
            if message:
                preview["warning"] = SENT_TO_CUSTOMER
            return preview

        async def execute() -> dict[str, Any]:
            body: dict[str, Any] = {"contact_id": contact_id, "inbox_id": inbox_id}
            if message:
                body["message"] = {"content": message}
            data = await ctx.client.post_account("/conversations", body)
            data = data if isinstance(data, dict) else {}
            return {"conversation_id": data.get("id"), "status": data.get("status"), "inbox_id": data.get("inbox_id")}

        arguments = {"contact_id": contact_id, "inbox_id": inbox_id, "message": message}
        return await gated("create_conversation", arguments, confirm_token, build_preview, execute)

    # --- create_contact / update_contact -------------------------------------------------------------------------

    def contact_fields(email: str | None, phone_number: str | None, identifier: str | None) -> dict[str, str]:
        for name, value in (("email", email), ("identifier", identifier)):
            _no_blank(value, f"The {name}")
        if email is not None and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ToolError("The email does not look like an email address.")
        if phone_number is not None and not PHONE.fullmatch(phone_number):
            raise ToolError("The phone number must be in international format with no spaces, like +15550100.")
        given = {"email": email, "phone_number": phone_number, "identifier": identifier}
        return {key: value for key, value in given.items() if value is not None}

    @mcp.tool(annotations=ACTION_ADDS, tags={TIER_ACTION, "module:actions"})
    @reporting_errors
    async def create_contact(
        name: Annotated[str, Field(min_length=1, max_length=200, description="The customer's name.")],
        email: Email = None,
        phone_number: Phone = None,
        identifier: Identifier = None,
        confirm_token: ConfirmToken = None,
    ) -> dict[str, Any]:
        """Create a customer. Give at least one way to reach them: email, phone_number or identifier."""
        fields = contact_fields(email, phone_number, identifier)
        if not fields:
            raise ToolError("Give at least one of email, phone_number or identifier.")
        _no_blank(name, "The name")

        async def build_preview() -> dict[str, Any]:
            return {"new_contact": {"name": name, **fields}}

        async def execute() -> dict[str, Any]:
            data = await ctx.client.post_account("/contacts", {"name": name, **fields})
            contact = _payload(data).get("contact")
            return shaping.compact_contact(contact if isinstance(contact, dict) else {})

        arguments = {"name": name, **fields}
        return await gated("create_contact", arguments, confirm_token, build_preview, execute)

    @mcp.tool(annotations=ACTION_OVERWRITES, tags={TIER_ACTION, "module:actions"})
    @reporting_errors
    async def update_contact(
        contact_id: ContactId,
        name: Annotated[str | None, Field(min_length=1, max_length=200, description="The new name.")] = None,
        email: Email = None,
        phone_number: Phone = None,
        identifier: Identifier = None,
        confirm_token: ConfirmToken = None,
    ) -> dict[str, Any]:
        """Change a customer's name, email, phone number or identifier. Only the fields you give are changed."""
        _no_blank(name, "The name")
        fields = contact_fields(email, phone_number, identifier)
        if name is not None:
            fields = {"name": name, **fields}
        if not fields:
            raise ToolError("Give at least one field to change.")

        async def build_preview() -> dict[str, Any]:
            current = _payload(await ctx.client.get_account(f"/contacts/{contact_id}"))
            changes = {
                key: {"from": current.get(key), "to": value}
                for key, value in fields.items()
                if current.get(key) != value
            }
            if not changes:
                raise ToolError("The customer already has these values. Nothing would change.")
            return {"contact": _person(current, "id", "name", "email"), "changes": changes}

        async def execute() -> dict[str, Any]:
            data = await ctx.client.put_account(f"/contacts/{contact_id}", fields)
            return shaping.compact_contact(_payload(data))

        arguments = {"contact_id": contact_id, **fields}
        return await gated("update_contact", arguments, confirm_token, build_preview, execute)
