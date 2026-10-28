"""Laptop (stdio) mode: one person, one key from the environment."""

from __future__ import annotations

from collections.abc import Callable


def static_token(token: str | None) -> Callable[[], str | None]:
    cleaned = (token or "").strip() or None
    return lambda: cleaned
