"""Pieces shared by every tool module."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from chatwoot_mcp.client import ChatwootClient, ChatwootError
from chatwoot_mcp.config import Settings

P = ParamSpec("P")
R = TypeVar("R")

TIER_READ = "tier:read"
TIER_WRITE = "tier:write"

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
SAFE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
SAFE_IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)


@dataclass(frozen=True)
class ToolContext:
    client: ChatwootClient
    settings: Settings


def reporting_errors(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Turn a failed Chatwoot call into a plain tool error the assistant can read and relay."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except ChatwootError as error:
            raise ToolError(str(error)) from None

    return wrapper


def page_info(page: int, truncated: bool, maximum: int) -> dict[str, Any]:
    info: dict[str, Any] = {"page": page}
    if truncated:
        info["truncated"] = f"Only the first {maximum} items are shown. Narrow the search to see the rest."
    return info
