"""Settings read from the environment. Every value is checked once, at start-up, so a typo stops the
server instead of showing up later as a confusing error inside a tool call."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

_TRUE = {"1", "true"}
_FALSE = {"", "0", "false"}


class ConfigError(ValueError):
    """A setting is missing or has an unusable value."""


def _integer(
    env: Mapping[str, str], name: str, *, default: int | None, minimum: int = 1, maximum: int | None = None
) -> int:
    raw = env.get(name)
    if raw is None:
        if default is None:
            raise ConfigError(f"{name} is required")
        return default
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < minimum or (maximum is not None and int(raw) > maximum):
        limit = f" and at most {maximum}" if maximum else ""
        raise ConfigError(f"{name} must be a whole number of at least {minimum}{limit}, got {raw!r}")
    return int(raw)


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _seconds(env: Mapping[str, str], name: str, *, default: float, maximum: float) -> float:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number of seconds, got {raw!r}") from None
    if not 0 < value <= maximum:
        raise ConfigError(f"{name} must be more than 0 and at most {maximum:g} seconds, got {raw!r}")
    return value


@dataclass(frozen=True)
class Settings:
    base_url: str
    account_id: int
    enable_writes: bool = False
    enable_actions: bool = False
    confirm_ttl_seconds: int = 300
    max_items: int = 25
    max_text_chars: int = 2000
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        base_url = env.get("CHATWOOT_BASE_URL", "").strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", base_url):
            raise ConfigError(f"CHATWOOT_BASE_URL must be an http:// or https:// address, got {base_url!r}")
        return cls(
            base_url=base_url,
            account_id=_integer(env, "CHATWOOT_ACCOUNT_ID", default=None),
            enable_writes=_flag(env, "CHATWOOT_MCP_ENABLE_WRITES"),
            enable_actions=_flag(env, "CHATWOOT_MCP_ENABLE_ACTIONS"),
            confirm_ttl_seconds=_integer(
                env, "CHATWOOT_MCP_CONFIRM_TTL_SECONDS", default=300, minimum=30, maximum=3600
            ),
            max_items=_integer(env, "CHATWOOT_MCP_MAX_ITEMS", default=25, maximum=100000),
            max_text_chars=_integer(env, "CHATWOOT_MCP_MAX_TEXT_CHARS", default=2000, maximum=100000),
            timeout_seconds=_seconds(env, "CHATWOOT_MCP_TIMEOUT_SECONDS", default=30.0, maximum=600.0),
        )
