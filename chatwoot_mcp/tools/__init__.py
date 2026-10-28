"""One module per area, each with `register(mcp, ctx)`.

Every tool carries all four MCP annotation hints, a `tier:*` tag and a `module:*` tag. The two write tools are
only registered when CHATWOOT_MCP_ENABLE_WRITES=true; otherwise they do not exist for the assistant at all.
"""

from __future__ import annotations

from fastmcp import FastMCP

from chatwoot_mcp.tools import contacts, conversations, lookups, profile, reports, writes
from chatwoot_mcp.tools.support import ToolContext

__all__ = ["ToolContext", "register_tools"]

READ_MODULES = (profile, conversations, contacts, lookups, reports)


def register_tools(mcp: FastMCP, ctx: ToolContext) -> None:
    for module in READ_MODULES:
        module.register(mcp, ctx)
    if ctx.settings.enable_writes:
        writes.register(mcp, ctx)
