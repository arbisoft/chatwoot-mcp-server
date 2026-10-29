"""MCP tool modules, one per Chatwoot area.

Each module exposes ``register(mcp, context)``. Every tool carries all four MCP ``ToolAnnotations`` hints, a
``tier:*`` tag and a ``module:*`` tag, and returns an ``Error: ...`` string instead of raising.

A module whose ``CHATWOOT_ENABLE_<MODULE>`` flag is false never registers, and ``CHATWOOT_READ_ONLY`` keeps every
write tool unregistered. ``get_me`` is always on. ``replies`` and ``macros`` reach the customer, so they are off by
default and go through a confirm step.
"""

from __future__ import annotations

from types import ModuleType

from fastmcp import FastMCP

from chatwoot_mcp.config import Settings
from chatwoot_mcp.tools import (
    contacts,
    conversations,
    help_center,
    labels,
    macros,
    me,
    messages,
    reference,
    replies,
    reports,
)
from chatwoot_mcp.tools.context import ToolContext

__all__ = ["MODULES", "ToolContext", "register_tools"]

MODULES: dict[str, ModuleType] = {
    "reference": reference,
    "conversations": conversations,
    "messages": messages,
    "contacts": contacts,
    "labels": labels,
    "help_center": help_center,
    "reports": reports,
    "replies": replies,
    "macros": macros,
}


def register_tools(mcp: FastMCP, context: ToolContext, settings: Settings) -> None:
    me.register(mcp, context)
    for name, module in MODULES.items():
        if settings.module_enabled(name):
            module.register(mcp, context)
