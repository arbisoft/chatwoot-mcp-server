"""Replies that reach the customer: ``prepare_send_reply`` then ``send_reply``, and ``prepare_create_conversation``
then ``create_conversation``. Off by default (``CHATWOOT_ENABLE_REPLIES=true``), and absent when read-only.

A message cannot be unsent, so sending takes two calls. The ``prepare_*`` tool changes nothing: it looks up what the
message would go to and returns a preview and a confirmation token bound to the caller, the action and the exact
arguments. The confirming tool spends the token and sends. A reply POST is never retried once it may have left, and
a failure says whether the message surely did not go, may have gone, or went.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.http import accepted_but_unreadable, never_sent, outcome_unknown
from chatwoot_mcp.client.models import Conversation, Inbox, Message, Person, payload
from chatwoot_mcp.shaping import compact, shape_message, shape_person
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import (
    CREATE,
    IRREVERSIBLE,
    PREPARE,
    ToolContext,
    as_dict,
    confirmation_refusal,
    prepared,
)

MODULE = "replies"
SEND_REPLY = "send_reply"
CREATE_CONVERSATION = "create_conversation"
MAX_MESSAGE_CHARS = 5000
SENT_TO_CUSTOMER = "This message will be sent to the customer and cannot be unsent."

ConversationId = Annotated[int, Field(ge=1, description="The conversation number shown in Chatwoot.")]
ContactId = Annotated[int, Field(ge=1, description="The customer's id. See search_contacts.")]
InboxId = Annotated[int, Field(ge=1, description="The inbox to start the chat in. See list_inboxes.")]
Content = Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS, description="The message text.")]
FirstMessage = Annotated[
    str | None,
    Field(min_length=1, max_length=MAX_MESSAGE_CHARS, description="An optional first message to the customer."),
]
ConfirmationToken = Annotated[str, Field(min_length=1, max_length=200, description="From the prepare tool.")]


def send_failure(error: ChatwootError, what: str) -> str:
    """Say whether the message went: surely not, maybe, or yes with an unreadable answer."""
    if accepted_but_unreadable(error):
        return f"Error: Chatwoot accepted the {what}, so it was sent, but its answer could not be read; check the chat"
    if never_sent(error):
        return f"{to_tool_error(error)}; nothing was sent, prepare it again"
    if outcome_unknown(error):
        return (
            f"{to_tool_error(error)}; the {what} may or may not have been sent. "
            "Check the chat before preparing it again"
        )
    return to_tool_error(error)


def register(mcp: FastMCP, context: ToolContext) -> None:
    if not context.writes_enabled:
        return

    def reply_payload(conversation_id: int, content: str) -> dict[str, Any]:
        return {"conversation_id": conversation_id, "content": content}

    @mcp.tool(**context.tool("Prepare to send reply", PREPARE, module=MODULE, tier=Tier.AGENT))
    async def prepare_send_reply(conversation_id: ConversationId, content: Content) -> dict[str, Any] | str:
        """Preview a reply to the customer and get a confirmation token. Nothing is sent yet.

        Show the preview to the user. Only once they approve it, call send_reply with the token and the same
        conversation_id and content.
        """
        if not content.strip():
            return "Error: the message is empty"
        try:
            session = await context.session()
            chat = Conversation.parse(await session.get(f"/conversations/{conversation_id}"))
        except ChatwootError as error:
            return to_tool_error(error)
        if chat.can_reply is False:
            return "Error: this chat cannot be replied to right now (for example the channel's reply window has closed)"
        preview = compact(
            {
                "conversation_id": conversation_id,
                "to": shape_person(chat.meta.sender, source="contact"),
                "channel": chat.meta.channel,
                "message": content,
                "warning": SENT_TO_CUSTOMER,
                "url": context.links.conversation(conversation_id),
            }
        )
        return await prepared(
            context, session, action=SEND_REPLY, payload=reply_payload(conversation_id, content), preview=preview
        )

    @mcp.tool(**context.tool("Send reply", IRREVERSIBLE, module=MODULE, tier=Tier.AGENT))
    async def send_reply(
        confirmation_token: ConfirmationToken, conversation_id: ConversationId, content: Content
    ) -> dict[str, Any] | str:
        """Send a reply the user approved, from prepare_send_reply's preview. The token works once."""
        try:
            session = await context.session()
        except ChatwootError as error:
            return to_tool_error(error)
        refusal = await confirmation_refusal(
            context, session, confirmation_token, action=SEND_REPLY, payload=reply_payload(conversation_id, content)
        )
        if refusal:
            return refusal
        body = {"content": content, "message_type": "outgoing", "private": False}
        try:
            message = Message.parse(await session.post(f"/conversations/{conversation_id}/messages", body))
        except ChatwootError as error:
            return send_failure(error, "reply")
        if message.private:
            return "Error: Chatwoot marked the message private, so the customer did not get it; check the chat"
        return {
            **shape_message(message, max_chars=context.max_chars),
            "url": context.links.conversation(conversation_id),
        }

    def conversation_payload(contact_id: int, inbox_id: int, message: str | None) -> dict[str, Any]:
        return {"contact_id": contact_id, "inbox_id": inbox_id, "message": message}

    @mcp.tool(**context.tool("Prepare to create conversation", PREPARE, module=MODULE, tier=Tier.AGENT))
    async def prepare_create_conversation(
        contact_id: ContactId, inbox_id: InboxId, message: FirstMessage = None
    ) -> dict[str, Any] | str:
        """Preview starting a new chat with a customer, optionally with a first message they receive. Nothing changes.

        Show the preview to the user. Only once they approve it, call create_conversation with the token and the
        same arguments.
        """
        if message is not None and not message.strip():
            return "Error: the message is empty"
        try:
            session = await context.session()
            contact = Person.parse(payload(await session.get(f"/contacts/{contact_id}")))
            inbox = Inbox.parse(await session.get(f"/inboxes/{inbox_id}"))
        except ChatwootError as error:
            return to_tool_error(error)
        preview = compact(
            {
                "contact": shape_person(contact, source="contact"),
                "inbox": {"id": inbox.id, "name": inbox.name, "channel_type": inbox.channel_type},
                "first_message": message,
                "warning": SENT_TO_CUSTOMER if message else None,
            }
        )
        return await prepared(
            context,
            session,
            action=CREATE_CONVERSATION,
            payload=conversation_payload(contact_id, inbox_id, message),
            preview=preview,
        )

    @mcp.tool(**context.tool("Create conversation", CREATE, module=MODULE, tier=Tier.AGENT))
    async def create_conversation(
        confirmation_token: ConfirmationToken, contact_id: ContactId, inbox_id: InboxId, message: FirstMessage = None
    ) -> dict[str, Any] | str:
        """Start the chat the user approved, from prepare_create_conversation's preview. The token works once."""
        try:
            session = await context.session()
        except ChatwootError as error:
            return to_tool_error(error)
        refusal = await confirmation_refusal(
            context,
            session,
            confirmation_token,
            action=CREATE_CONVERSATION,
            payload=conversation_payload(contact_id, inbox_id, message),
        )
        if refusal:
            return refusal
        body: dict[str, Any] = {"contact_id": contact_id, "inbox_id": inbox_id}
        if message:
            body["message"] = {"content": message}
        try:
            created = as_dict(await session.post("/conversations", body))
        except ChatwootError as error:
            return send_failure(error, "new conversation")
        conversation_id = created.get("id")
        return compact(
            {
                "conversation_id": conversation_id,
                "status": created.get("status"),
                "inbox_id": created.get("inbox_id"),
                "url": context.links.conversation(conversation_id) if isinstance(conversation_id, int) else None,
            }
        )
