"""Reference lists: the names and ids other tools need (inboxes, agents, teams, labels, canned responses, custom
fields) and the account itself. Names administrators write (inboxes, teams, labels, custom fields) are returned as
they are. Agents set their own names and any agent can write a canned response, so those are framed as untrusted."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import (
    Account,
    Agent,
    CannedResponse,
    ChatwootModel,
    CustomAttributeDefinition,
    Inbox,
    Label,
    Team,
)
from chatwoot_mcp.shaping import compact, truncate, untrusted_field
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, ToolContext, limited

MODULE = "reference"


def register(mcp: FastMCP, context: ToolContext) -> None:
    async def listing(path: str, key: str, model: type[ChatwootModel], fields: tuple[str, ...], **params: Any):
        session = await context.session()
        items, cut = limited(model.parse_rows(await session.get(path, params or None)), context.max_items)
        out: dict[str, Any] = {key: [compact(item.model_dump(include=set(fields))) for item in items]}
        if cut:
            out["truncated"] = f"Only the first {context.max_items} items are shown."
        return out

    def framed_agents(out: dict[str, Any], key: str) -> dict[str, Any]:
        def frame(row: dict[str, Any], field: str) -> str | None:
            return untrusted_field(row.get(field), source=f"agent_{field}", item_id=row.get("id"))

        out[key] = [compact({**row, "name": frame(row, "name"), "email": frame(row, "email")}) for row in out[key]]
        return out

    @mcp.tool(**context.tool("List inboxes", READ, module=MODULE, tier=Tier.AGENT))
    async def list_inboxes() -> dict[str, Any] | str:
        """List the inboxes (website chat, email, WhatsApp and so on) with their ids."""
        try:
            return await listing(
                "/inboxes", "inboxes", Inbox, ("id", "name", "channel_type", "website_url", "phone_number")
            )
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("Get inbox", READ, module=MODULE, tier=Tier.AGENT))
    async def get_inbox(
        inbox_id: Annotated[int, Field(ge=1, description="The inbox id. See list_inboxes.")],
    ) -> dict[str, Any] | str:
        """Show one inbox's settings: greeting, working hours, auto-assignment and survey switches."""
        try:
            session = await context.session()
            inbox = Inbox.parse(await session.get(f"/inboxes/{inbox_id}"))
        except ChatwootError as error:
            return to_tool_error(error)
        return compact(inbox.model_dump(include=set(Inbox.model_fields)))

    @mcp.tool(**context.tool("List agents", READ, module=MODULE, tier=Tier.AGENT))
    async def list_agents() -> dict[str, Any] | str:
        """List the support staff with their ids, roles and availability."""
        try:
            out = await listing("/agents", "agents", Agent, ("id", "name", "email", "role", "availability_status"))
        except ChatwootError as error:
            return to_tool_error(error)
        return framed_agents(out, "agents")

    @mcp.tool(**context.tool("List teams", READ, module=MODULE, tier=Tier.AGENT))
    async def list_teams() -> dict[str, Any] | str:
        """List the teams with their ids."""
        try:
            return await listing("/teams", "teams", Team, ("id", "name", "description"))
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("List team members", READ, module=MODULE, tier=Tier.AGENT))
    async def list_team_members(
        team_id: Annotated[int, Field(ge=1, description="The team id. See list_teams.")],
    ) -> dict[str, Any] | str:
        """List the agents in one team."""
        try:
            out = await listing(
                f"/teams/{team_id}/team_members",
                "members",
                Agent,
                ("id", "name", "email", "role", "availability_status"),
            )
        except ChatwootError as error:
            return to_tool_error(error)
        return {"team_id": team_id, **framed_agents(out, "members")}

    @mcp.tool(**context.tool("List labels", READ, module=MODULE, tier=Tier.AGENT))
    async def list_labels() -> dict[str, Any] | str:
        """List the labels (tags) that can be put on chats and customers."""
        try:
            return await listing("/labels", "labels", Label, ("id", "title", "description"))
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("List canned responses", READ, module=MODULE, tier=Tier.AGENT))
    async def list_canned_responses() -> dict[str, Any] | str:
        """List the saved quick replies: their short code and text."""
        try:
            out = await listing(
                "/canned_responses", "canned_responses", CannedResponse, ("id", "short_code", "content")
            )
        except ChatwootError as error:
            return to_tool_error(error)

        def frame(row: dict[str, Any]) -> dict[str, Any]:
            content, item_id = row.get("content"), row.get("id")
            text = truncate(content, context.max_chars).text if isinstance(content, str) else content
            return compact(
                {
                    **row,
                    "short_code": untrusted_field(row.get("short_code"), source="canned_short_code", item_id=item_id),
                    "content": untrusted_field(text, source="canned_response", item_id=item_id),
                }
            )

        out["canned_responses"] = [frame(row) for row in out["canned_responses"]]
        return out

    @mcp.tool(**context.tool("List custom attribute definitions", READ, module=MODULE, tier=Tier.AGENT))
    async def list_custom_attribute_definitions(
        model: Annotated[
            Literal["conversation", "contact"], Field(description="Fields on chats or on customers.")
        ] = "conversation",
    ) -> dict[str, Any] | str:
        """List the custom fields the team has defined, with their keys and allowed values."""
        try:
            return await listing(
                "/custom_attribute_definitions",
                "custom_attributes",
                CustomAttributeDefinition,
                tuple(CustomAttributeDefinition.model_fields),
                attribute_model=f"{model}_attribute",
            )
        except ChatwootError as error:
            return to_tool_error(error)

    @mcp.tool(**context.tool("Get account", READ, module=MODULE, tier=Tier.AGENT))
    async def get_account() -> dict[str, Any] | str:
        """Show the account's name, language, status and support address. No billing or settings."""
        try:
            session = await context.session()
            account = Account.parse(await session.get(""))
        except ChatwootError as error:
            return to_tool_error(error)
        return compact(account.model_dump(include=set(Account.model_fields)))
