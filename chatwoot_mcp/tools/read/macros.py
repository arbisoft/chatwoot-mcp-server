"""Macros, read: ``list_macros``. Off by default (``CHATWOOT_ENABLE_MACROS=true``). Macro names and steps are
written by administrators and returned as they are.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from chatwoot_mcp.client import ChatwootError, to_tool_error
from chatwoot_mcp.client.models import Macro
from chatwoot_mcp.shaping import shape_macro
from chatwoot_mcp.tiers import Tier
from chatwoot_mcp.tools.context import READ, ToolContext, limited

MODULE = "macros"


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("List macros", READ, module=MODULE, tier=Tier.AGENT))
    async def list_macros() -> dict[str, Any] | str:
        """List the macros you can run: saved sets of steps, such as label, assign and resolve, with their steps."""
        try:
            session = await context.session()
            macros = Macro.parse_rows(await session.get("/macros"))
        except ChatwootError as error:
            return to_tool_error(error)
        items, cut = limited(macros, context.max_items)
        out: dict[str, Any] = {"macros": [shape_macro(macro) for macro in items]}
        if cut:
            out["truncated"] = f"Only the first {context.max_items} macros are shown."
        return out
