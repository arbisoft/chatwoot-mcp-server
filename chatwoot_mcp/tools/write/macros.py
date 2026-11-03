"""Macros, write: ``prepare_run_macro`` then ``run_macro``. Off by default (``CHATWOOT_ENABLE_MACROS=true``).

A macro is a saved set of steps (label, assign, resolve, message the customer, ...), so running one takes two calls:
``prepare_run_macro`` lists every step and warns about the ones that reach the customer or leave Chatwoot, and
returns a token bound to the caller, the chat, the macro and its exact steps; ``run_macro`` reads the macro again and
spends the token only if the steps are unchanged.

Chatwoot runs a macro in a background job: ``execute`` answers at once and the steps happen shortly after. An edit to
the macro in that short window is not caught.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Conversation, Macro, payload
from chatwoot_mcp.shaping import macro_steps, shape_person
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import (
    IRREVERSIBLE,
    PREPARE,
    ConfirmationToken,
    ConversationId,
    ToolContext,
    confirmation_refusal,
    prepared,
    send_failure,
)

MODULE = "macros"
ACTION = "run_macro"
# Steps known to stay inside Chatwoot. Every other step, including ones Chatwoot adds later, gets a warning.
INTERNAL_STEPS = frozenset(
    {
        "add_label",
        "remove_label",
        "assign_agent",
        "remove_assigned_agent",
        "assign_team",
        "remove_assigned_team",
        "change_priority",
        "add_private_note",
        "snooze_conversation",
        "open_conversation",
        "pending_conversation",
    }
)
OUTWARD_STEPS = {
    "send_message": "sends a message to the customer",
    "send_attachment": "sends a file to the customer",
    "send_email_transcript": "emails the chat transcript",
    "send_webhook_event": "sends the chat's data to an outside web address",
    "resolve_conversation": "resolves the chat, which may send the customer a satisfaction survey",
    "mute_conversation": "resolves and mutes the chat, which may send the customer a satisfaction survey",
    "change_status": "changes the chat's status, which may resolve it and send the customer a satisfaction survey",
}

MacroId = Annotated[int, Field(ge=1, description="The macro to run. See list_macros.")]


def macro_payload(macro: Macro, conversation_id: int) -> dict[str, Any]:
    actions = [action.model_dump(mode="json") for action in macro.actions]
    return {"macro_id": macro.id, "conversation_id": conversation_id, "actions": actions}


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Prepare to run macro", PREPARE, module=MODULE, tier=Tier.AGENT))
    async def prepare_run_macro(macro_id: MacroId, conversation_id: ConversationId) -> dict[str, Any] | str:
        """Preview running a macro on one chat and get a confirmation token. Nothing changes yet.

        Show the preview to the user. Only once they approve it, call run_macro with the token and the same
        macro_id and conversation_id.
        """
        try:
            session = await context.session()
            macro_data, chat_data = await asyncio.gather(
                session.get(f"/macros/{macro_id}"), session.get(f"/conversations/{conversation_id}")
            )
            macro = Macro.parse(payload(macro_data))
            chat = Conversation.parse(chat_data)
        except ChatwootError as error:
            return to_tool_error(error)
        steps = macro_steps(macro)
        outward = [
            OUTWARD_STEPS.get(action.action_name, f"runs `{action.action_name}`, a step this server does not know")
            for action in macro.actions
            if action.action_name not in INTERNAL_STEPS
        ]
        preview: dict[str, Any] = {
            "conversation_id": conversation_id,
            "contact": shape_person(chat.meta.sender, source="contact"),
            "macro": macro.name,
            "steps": steps,
            "url": context.links.conversation(conversation_id),
        }
        if outward:
            preview["warning"] = f"This macro {', and '.join(outward)}. That cannot be undone."
        return await prepared(
            context, session, action=ACTION, payload=macro_payload(macro, conversation_id), preview=preview
        )

    @mcp.tool(**context.tool("Run macro", IRREVERSIBLE, module=MODULE, tier=Tier.AGENT))
    async def run_macro(
        confirmation_token: ConfirmationToken,
        macro_id: MacroId,
        conversation_id: ConversationId,
    ) -> dict[str, Any] | str:
        """Run a macro the user approved, from prepare_run_macro's preview. The token works once."""
        try:
            session = await context.session()
            macro = Macro.parse(payload(await session.get(f"/macros/{macro_id}")))
        except ChatwootError as error:
            return to_tool_error(error)
        refusal = await confirmation_refusal(
            context, session, confirmation_token, action=ACTION, payload=macro_payload(macro, conversation_id)
        )
        if refusal:
            return f"{refusal} (if the macro was edited since the preview, its steps no longer match)"
        try:
            await session.post(f"/macros/{macro_id}/execute", {"conversation_ids": [conversation_id]})
        except ChatwootError as error:
            return send_failure(error, "macro")
        return {
            "macro_id": macro_id,
            "conversation_id": conversation_id,
            "status": "started",
            "note": "Chatwoot runs macros in the background. Read the chat again in a moment to see the result.",
            "url": context.links.conversation(conversation_id),
        }
