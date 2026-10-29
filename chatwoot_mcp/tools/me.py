"""``get_me``: who this connection acts as. Always on, visible to every tier, and never shows any key."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Profile
from chatwoot_mcp.credentials.base import PROFILE_PATH
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, ToolContext

MODULE = "me"


def register(mcp: FastMCP, context: ToolContext) -> None:
    configured = context.settings.account_id

    @mcp.tool(**context.tool("Get me", READ, module=MODULE, tier=Tier.NONE))
    async def get_me() -> dict[str, Any] | str:
        """Show who this connection acts as in Chatwoot, their role, and which account the tools work in."""
        try:
            session = await context.session()
            profile = Profile.parse(await session.client.get(PROFILE_PATH, token=session.credential.token))
        except ChatwootError as error:
            return to_tool_error(error)
        # The profile also holds access and pubsub tokens. Only the fields below are ever returned.
        return {
            "id": profile.id,
            "name": profile.name,
            "email": profile.email,
            "availability_status": profile.availability_status,
            "accounts": [{"id": a.id, "name": a.name, "role": a.role} for a in profile.accounts],
            "tools_work_in_account": configured,
            "tier": session.tier.name.lower() if session.tier is not None else "unknown",
        }
