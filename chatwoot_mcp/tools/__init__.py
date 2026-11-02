"""MCP tools, split into ``read/`` (only reads Chatwoot) and ``write/`` (changes something), one module per area.

Each module exposes ``register(mcp, context)``. Every tool carries all four MCP ``ToolAnnotations`` hints, a
``tier:*`` tag and a ``module:*`` tag, and returns an ``Error: ...`` string instead of raising.

An area whose ``CHATWOOT_ENABLE_<MODULE>`` flag is false registers neither half, and ``CHATWOOT_READ_ONLY`` keeps
every ``write/`` module unregistered. ``get_me`` is always on. ``replies`` and ``macros`` reach the customer, so they
are off by default and go through a confirm step.
"""

from __future__ import annotations

from types import ModuleType

from fastmcp import FastMCP

from chatwoot_mcp.config import Settings
from chatwoot_mcp.tools import read, write
from chatwoot_mcp.tools.context import ToolContext

__all__ = ["READ_MODULES", "WRITE_MODULES", "ToolContext", "register_tools"]

READ_MODULES: dict[str, ModuleType] = {
    "reference": read.reference,
    "conversations": read.conversations,
    "messages": read.messages,
    "contacts": read.contacts,
    "labels": read.labels,
    "help_center": read.help_center,
    "reports": read.reports,
    "macros": read.macros,
}
WRITE_MODULES: dict[str, ModuleType] = {
    "conversations": write.conversations,
    "messages": write.messages,
    "contacts": write.contacts,
    "labels": write.labels,
    "replies": write.replies,
    "macros": write.macros,
}


def register_tools(mcp: FastMCP, context: ToolContext, settings: Settings) -> None:
    read.me.register(mcp, context)
    for name, module in READ_MODULES.items():
        if settings.module_enabled(name):
            module.register(mcp, context)
    if not context.writes_enabled:
        return
    for name, module in WRITE_MODULES.items():
        if settings.module_enabled(name):
            module.register(mcp, context)
