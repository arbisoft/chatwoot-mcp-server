"""Shared parsing for the server's boolean environment flags.

Every gate (``CHATWOOT_READ_ONLY``, ``CHATWOOT_ENABLE_CONVERSATIONS``,
``CHATWOOT_FILTER_TOOLS_BY_ROLE``, ...) is read through :func:`parse_flag` so
that all of them agree on what "true" means. Values are stripped before
comparison: ``CHATWOOT_READ_ONLY="true "`` with a trailing space is routine in
Docker ``.env`` files, and must not silently fall back to "false" and re-expose
write tools.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

_TRUTHY = ("true", "1", "yes", "on")


def parse_flag(raw: str | None, default: bool = False) -> bool:
    """``True`` when the stripped, lower-cased value is true, 1, yes or on; ``default`` when unset or blank."""
    value = (raw or "").strip().lower()
    if not value:
        return default
    return value in _TRUTHY


def env_flag(name: str, default: bool = False, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return parse_flag(env.get(name), default)
