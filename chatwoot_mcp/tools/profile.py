"""Who the server thinks you are. Always available, and never shows any key."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from chatwoot_mcp.tools.support import READ_ONLY, TIER_READ, ToolContext, reporting_errors


def register(mcp: FastMCP, ctx: ToolContext) -> None:
    configured = ctx.settings.account_id

    @mcp.tool(annotations=READ_ONLY, tags={TIER_READ, "module:profile"})
    @reporting_errors
    async def get_my_profile() -> dict[str, Any]:
        """Show who this connection acts as in Chatwoot, and which account the tools work in."""
        data = await ctx.client.get_profile()
        data = data if isinstance(data, dict) else {}
        accounts = [
            {"id": a.get("id"), "name": a.get("name"), "role": a.get("role")}
            for a in data.get("accounts") or []
            if isinstance(a, dict)
        ]
        # The profile also holds access and pubsub tokens. Only the fields below are ever returned.
        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "email": data.get("email"),
            "availability_status": data.get("availability_status"),
            "accounts": accounts,
            "tools_work_in_account": configured,
            "has_access_to_that_account": any(a["id"] == configured for a in accounts),
        }
