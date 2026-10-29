"""Platform-mode settings, read once from the environment.

Like :class:`chatwoot_mcp.config.Settings`, :meth:`PlatformSettings.from_env` is pure over the mapping it is given.
:func:`community_settings` derives the core settings so that every Chatwoot call goes to
``CHATWOOT_INTERNAL_BASE_URL``: the public host sits behind the SSO proxy, which puts a login in front of ``/api``.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from chatwoot_mcp.config import ConfigError, Settings, positive_int_setting, read_setting
from chatwoot_mcp.env_flags import env_flag

DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 86400
REFRESH_TOKEN_FALLBACK_SECONDS = 30 * 24 * 60 * 60
DEFAULT_SCOPES = ("openid",)
# The MCP clients people use, as in Pressingly/plane-mcp-server. MCP_ALLOWED_CLIENT_REDIRECT_URIS adds to these, so
# onboarding a new client (Askii, say) is a config change, not a release.
DEFAULT_REDIRECT_URIS = (
    "http://localhost:*",
    "http://localhost:*/*",
    "http://127.0.0.1:*",
    "http://127.0.0.1:*/*",
    "cursor://anysphere.cursor-mcp/oauth/*",
    "https://www.cursor.com/*",
    "https://vscode.dev/redirect",
    "https://insiders.vscode.dev/redirect",
    "https://claude.ai/*",
    "https://chatgpt.com/connector/oauth/*",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://oauth-redirect.googleusercontent.com/r/*",
    # Askii, Moneta's own assistant (the address surfsense-mcp-server's example allows).
    "https://askii.moneta.dev/*",
)
REQUIRED = (
    "MCP_BASE_URL",
    "COGNITO_USER_POOL_ID",
    "COGNITO_AWS_REGION",
    "OIDC_CLIENT_ID",
    "CHATWOOT_INTERNAL_BASE_URL",
    "DEFAULT_EMAIL_DOMAIN",
)
HOSTNAME = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
NAMESPACE_LENGTH = 12
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _split(raw: str, *, on_spaces: bool = False) -> tuple[str, ...]:
    parts = raw.replace(",", " ").split() if on_spaces else raw.split(",")
    return tuple(part.strip() for part in parts if part.strip())


@dataclass(frozen=True)
class PlatformSettings:
    base_url: str
    user_pool_id: str
    aws_region: str
    client_id: str
    internal_url: str
    default_email_domain: str
    client_secret: str = field(default="", repr=False)
    jwt_signing_key: str = field(default="", repr=False)
    storage_url: str = field(default="", repr=False)
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    allowed_redirect_uris: tuple[str, ...] = DEFAULT_REDIRECT_URIS
    allow_any_redirect_uri: bool = False
    access_token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    upstream_auth_url: str = ""
    upstream_token_url: str = ""
    cors_origins: tuple[str, ...] = ("*",)
    log_level: str = "INFO"
    production: bool = False
    http_api_key_route: bool = False
    email_capture: bool = False

    @property
    def key_material(self) -> str:
        """Entropy for the Fernet keys: the client secret when there is one (confidential client)."""
        return self.client_secret or self.jwt_signing_key

    @property
    def redirect_allowlist(self) -> list[str] | None:
        """``None`` (only with the explicit opt-out) lets client registration accept any redirect URI."""
        return None if self.allow_any_redirect_uri else list(self.allowed_redirect_uris)

    @property
    def namespace(self) -> str:
        """Separates two deployments that share one Valkey database."""
        return hashlib.sha256(f"{self.client_id}|{self.internal_url}".encode()).hexdigest()[:NAMESPACE_LENGTH]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PlatformSettings:
        env = os.environ if environ is None else environ
        missing = [name for name in REQUIRED if not read_setting(env, name)]
        if missing:
            raise ConfigError("platform mode is missing required settings: " + ", ".join(missing))
        if not read_setting(env, "OIDC_CLIENT_SECRET") and not read_setting(env, "MCP_JWT_SIGNING_KEY"):
            raise ConfigError(
                "platform mode needs OIDC_CLIENT_SECRET (confidential client) or MCP_JWT_SIGNING_KEY (public client)"
            )
        if read_setting(env, "CHATWOOT_HTTP_TOKEN") or env_flag("CHATWOOT_HTTP_SHARED_TOKEN_ROUTE", environ=env):
            raise ConfigError(
                "platform mode never uses a shared key: unset CHATWOOT_HTTP_TOKEN and CHATWOOT_HTTP_SHARED_TOKEN_ROUTE"
            )
        extra_redirect_uris = _split(read_setting(env, "MCP_ALLOWED_CLIENT_REDIRECT_URIS"))
        allowed_redirect_uris = tuple(dict.fromkeys(DEFAULT_REDIRECT_URIS + extra_redirect_uris))
        allow_any_redirect_uri = env_flag("MCP_ALLOW_ANY_REDIRECT_URI", environ=env)
        default_email_domain = read_setting(env, "DEFAULT_EMAIL_DOMAIN").lower()
        if not HOSTNAME.match(default_email_domain):
            raise ConfigError(
                f"DEFAULT_EMAIL_DOMAIN={default_email_domain!r} must be a bare domain such as askii.ai "
                "(no @, scheme, path or trailing dot)"
            )
        log_level = read_setting(env, "MCP_LOG_LEVEL").upper()
        return cls(
            base_url=read_setting(env, "MCP_BASE_URL").rstrip("/"),
            user_pool_id=read_setting(env, "COGNITO_USER_POOL_ID"),
            aws_region=read_setting(env, "COGNITO_AWS_REGION"),
            client_id=read_setting(env, "OIDC_CLIENT_ID"),
            internal_url=read_setting(env, "CHATWOOT_INTERNAL_BASE_URL").rstrip("/"),
            default_email_domain=default_email_domain,
            client_secret=read_setting(env, "OIDC_CLIENT_SECRET"),
            jwt_signing_key=read_setting(env, "MCP_JWT_SIGNING_KEY"),
            storage_url=read_setting(env, "MCP_OAUTH_STORAGE_URL"),
            scopes=_split(read_setting(env, "MCP_OIDC_SCOPES"), on_spaces=True) or DEFAULT_SCOPES,
            allowed_redirect_uris=allowed_redirect_uris,
            allow_any_redirect_uri=allow_any_redirect_uri,
            access_token_ttl_seconds=positive_int_setting(
                env, "MCP_ACCESS_TOKEN_TTL_SECONDS", DEFAULT_ACCESS_TOKEN_TTL_SECONDS
            ),
            upstream_auth_url=read_setting(env, "COGNITO_UPSTREAM_AUTH_URL"),
            upstream_token_url=read_setting(env, "COGNITO_UPSTREAM_TOKEN_URL"),
            cors_origins=_split(read_setting(env, "MCP_ALLOWED_ORIGINS")) or ("*",),
            log_level=log_level if log_level in LOG_LEVELS else "INFO",
            production=read_setting(env, "MCP_ENV").lower() == "production",
            http_api_key_route=env_flag("CHATWOOT_HTTP_API_KEY_ROUTE", environ=env),
            email_capture=env_flag("LAUNCHPAD_EMAIL_CAPTURE", environ=env),
        )


def community_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Core settings pointed at the internal URL, with ``CHATWOOT_URL`` (if any) kept for links."""
    env = os.environ if environ is None else environ
    public = read_setting(env, "CHATWOOT_PUBLIC_URL") or read_setting(env, "CHATWOOT_URL")
    return Settings.from_env(
        {**env, "CHATWOOT_URL": read_setting(env, "CHATWOOT_INTERNAL_BASE_URL"), "CHATWOOT_PUBLIC_URL": public}
    )
