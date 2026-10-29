"""Macros: ``list_macros``, then ``prepare_run_macro`` and ``run_macro``. Off by default
(``CHATWOOT_ENABLE_MACROS=true``); the run tools are absent when read-only.

A macro is a saved set of steps (label, assign, resolve, message the customer, ...), so running one takes two calls:
``prepare_run_macro`` lists every step and warns about the ones that reach the customer or leave Chatwoot, and
returns a token bound to the caller, the macro and the chat; ``run_macro`` spends it.

Chatwoot runs a macro in a background job: ``execute`` answers at once and the steps happen shortly after. Macro
names and steps are written by administrators and returned as they are.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Conversation, Macro, payload
from chatwoot_mcp.shaping import macro_steps, shape_macro, shape_person
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import (
    IRREVERSIBLE,
    PREPARE,
    READ,
    ConversationId,
    ToolContext,
    confirmation_refusal,
    prepared,
)
from chatwoot_mcp.tools.replies import send_failure

MODULE = "macros"
ACTION = "run_macro"
# Macro steps that reach the customer or leave Chatwoot.
OUTWARD_STEPS = {
    "send_message": "sends a message to the customer",
    "send_attachment": "sends a file to the customer",
    "send_email_transcript": "emails the chat transcript",
    "send_webhook_event": "sends the chat's data to an outside web address",
}

MacroId = Annotated[int, Field(ge=1, description="The macro to run. See list_macros.")]


def macro_payload(macro_id: int, conversation_id: int) -> dict[str, Any]:
    return {"macro_id": macro_id, "conversation_id": conversation_id}


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("List macros", READ, module=MODULE, tier=Tier.AGENT))
    async def list_macros() -> dict[str, Any] | str:
        """List the macros you can run: saved sets of steps, such as label, assign and resolve, with their steps."""
        try:
            session = await context.session()
            macros = Macro.parse_rows(await session.get("/macros"))
        except ChatwootError as error:
            return to_tool_error(error)
        return {"macros": [shape_macro(macro) for macro in macros[: context.max_items]]}

    if not context.writes_enabled:
        return

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
        outward = [OUTWARD_STEPS[action.action_name] for action in macro.actions if action.action_name in OUTWARD_STEPS]
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
            context, session, action=ACTION, payload=macro_payload(macro_id, conversation_id), preview=preview
        )

    @mcp.tool(**context.tool("Run macro", IRREVERSIBLE, module=MODULE, tier=Tier.AGENT))
    async def run_macro(
        confirmation_token: Annotated[str, Field(min_length=1, max_length=200, description="From prepare_run_macro.")],
        macro_id: MacroId,
        conversation_id: ConversationId,
    ) -> dict[str, Any] | str:
        """Run a macro the user approved, from prepare_run_macro's preview. The token works once."""
        try:
            session = await context.session()
        except ChatwootError as error:
            return to_tool_error(error)
        refusal = await confirmation_refusal(
            context, session, confirmation_token, action=ACTION, payload=macro_payload(macro_id, conversation_id)
        )
        if refusal:
            return refusal
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
