"""Runtime settings, read once from the environment.

``Settings.from_env`` is pure over the mapping it is given, so tests pass a
dict instead of patching ``os.environ``. Every value is checked here, so a typo
stops the server at start-up instead of surfacing later inside a tool call.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from chatwoot_mcp.env_flags import env_flag

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8214
DEFAULT_CONFIRM_TTL_SECONDS = 600
DEFAULT_MAX_ITEMS = 25
DEFAULT_MAX_TEXT_CHARS = 2000
TOOL_MODULES = (
    "reference",
    "conversations",
    "messages",
    "contacts",
    "labels",
    "help_center",
    "reports",
    "replies",
    "macros",
)
# Both reach the customer (or run many steps at once), so they are opt-in, like Zammad's email and macros.
DEFAULT_OFF_MODULES = frozenset({"replies", "macros"})
DEFAULT_ENABLED_MODULES = frozenset(TOOL_MODULES) - DEFAULT_OFF_MODULES
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_URL = re.compile(r"https?://[^\s/]+(/\S*)?")


class ConfigError(ValueError):
    """Raised when a required setting is missing or malformed."""


@dataclass(frozen=True)
class Settings:
    chatwoot_url: str
    account_id: int
    http_token: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS
    http_host: str = DEFAULT_HTTP_HOST
    http_port: int = DEFAULT_HTTP_PORT
    public_url: str = ""
    read_only: bool = False
    filter_tools_by_role: bool = False
    confirm_ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS
    max_items: int = DEFAULT_MAX_ITEMS
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    enabled_modules: frozenset[str] = DEFAULT_ENABLED_MODULES
    http_shared_token_route: bool = False

    @property
    def browser_url(self) -> str:
        return self.public_url or self.chatwoot_url

    def module_enabled(self, module: str) -> bool:
        return module in self.enabled_modules

    def check_http(self) -> None:
        """Refuse an HTTP setup that would hand ``CHATWOOT_HTTP_TOKEN`` to anyone who can reach ``/mcp``."""
        if self.http_token and not self.http_shared_token_route:
            raise ConfigError(
                "CHATWOOT_HTTP_TOKEN is set in http mode: /mcp would act as that key's owner for anyone who can "
                "reach it. Unset it and have each user send X-Chatwoot-Token to /http/api-key/mcp, or set "
                "CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true to accept that"
            )
        if self.http_shared_token_route and not self.http_token:
            raise ConfigError("CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true needs CHATWOOT_HTTP_TOKEN")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        chatwoot_url = _read(env, "CHATWOOT_URL").rstrip("/")
        if not _URL.fullmatch(chatwoot_url):
            raise ConfigError(f"CHATWOOT_URL must be an http:// or https:// address, got {chatwoot_url!r}")
        public_url = _read(env, "CHATWOOT_PUBLIC_URL").rstrip("/")
        if public_url and not _URL.fullmatch(public_url):
            raise ConfigError(f"CHATWOOT_PUBLIC_URL must be an http:// or https:// address, got {public_url!r}")
        return cls(
            chatwoot_url=chatwoot_url,
            account_id=_required_int(env, "CHATWOOT_ACCOUNT_ID"),
            http_token=_read(env, "CHATWOOT_HTTP_TOKEN") or None,
            timeout_seconds=_positive_float(env, "CHATWOOT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            connect_timeout_seconds=_positive_float(
                env, "CHATWOOT_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS
            ),
            http_host=_read(env, "MCP_HTTP_HOST") or DEFAULT_HTTP_HOST,
            http_port=_port(env, "MCP_HTTP_PORT", DEFAULT_HTTP_PORT),
            public_url=public_url,
            read_only=env_flag("CHATWOOT_READ_ONLY", environ=env),
            filter_tools_by_role=env_flag("CHATWOOT_FILTER_TOOLS_BY_ROLE", environ=env),
            confirm_ttl_seconds=_positive_int(env, "CHATWOOT_CONFIRM_TTL_SECONDS", DEFAULT_CONFIRM_TTL_SECONDS),
            max_items=_positive_int(env, "CHATWOOT_MAX_ITEMS", DEFAULT_MAX_ITEMS),
            max_text_chars=_positive_int(env, "CHATWOOT_MAX_TEXT_CHARS", DEFAULT_MAX_TEXT_CHARS),
            enabled_modules=frozenset(
                module
                for module in TOOL_MODULES
                if env_flag(module_flag(module), default=module in DEFAULT_ENABLED_MODULES, environ=env)
            ),
            http_shared_token_route=env_flag("CHATWOOT_HTTP_SHARED_TOKEN_ROUTE", environ=env),
        )


def module_flag(module: str) -> str:
    return f"CHATWOOT_ENABLE_{module.upper()}"


def _read(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _read(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a finite number greater than zero")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise ConfigError(f"{name}={raw!r} must be a whole number of at least 1")
    return int(raw)


def _required_int(env: Mapping[str, str], name: str) -> int:
    if not _read(env, name):
        raise ConfigError(f"{name} is required")
    return _positive_int(env, name, 0)


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _read(env, name)
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= 65535:
        raise ConfigError(f"{name}={raw!r} is not a valid TCP port")
    return int(raw)
