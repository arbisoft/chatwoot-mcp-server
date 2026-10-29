import pytest

from chatwoot_mcp.config import (
    DEFAULT_ENABLED_MODULES,
    ConfigError,
    Settings,
    module_flag,
)
from chatwoot_mcp.env_flags import parse_flag

BASE_ENV = {"CHATWOOT_URL": "https://chat.example.com/", "CHATWOOT_ACCOUNT_ID": "3"}


def test_defaults():
    settings = Settings.from_env(BASE_ENV)
    assert settings.chatwoot_url == "https://chat.example.com"
    assert settings.account_id == 3
    assert settings.http_token is None and not settings.read_only and not settings.filter_tools_by_role
    assert settings.enabled_modules == DEFAULT_ENABLED_MODULES
    assert not settings.module_enabled("replies") and not settings.module_enabled("macros")
    assert settings.browser_url == "https://chat.example.com"


def test_every_value_is_read():
    settings = Settings.from_env(
        {
            **BASE_ENV,
            "CHATWOOT_HTTP_TOKEN": " key ",
            "CHATWOOT_PUBLIC_URL": "https://public.example.com/",
            "CHATWOOT_READ_ONLY": "yes",
            "CHATWOOT_FILTER_TOOLS_BY_ROLE": "1",
            "CHATWOOT_CONFIRM_TTL_SECONDS": "60",
            "CHATWOOT_MAX_ITEMS": "5",
            "CHATWOOT_MAX_TEXT_CHARS": "50",
            "CHATWOOT_TIMEOUT_SECONDS": "2.5",
            "CHATWOOT_CONNECT_TIMEOUT_SECONDS": "1",
            "MCP_HTTP_HOST": "0.0.0.0",
            "MCP_HTTP_PORT": "9000",
            "CHATWOOT_ENABLE_REPLIES": "true",
            "CHATWOOT_ENABLE_REPORTS": "false",
            "CHATWOOT_HTTP_SHARED_TOKEN_ROUTE": "on",
        }
    )
    assert settings.http_token == "key" and settings.browser_url == "https://public.example.com"
    assert settings.read_only and settings.filter_tools_by_role and settings.http_shared_token_route
    assert (settings.confirm_ttl_seconds, settings.max_items, settings.max_text_chars) == (60, 5, 50)
    assert (settings.timeout_seconds, settings.connect_timeout_seconds) == (2.5, 1.0)
    assert (settings.http_host, settings.http_port) == ("0.0.0.0", 9000)
    assert settings.module_enabled("replies") and not settings.module_enabled("reports")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"CHATWOOT_URL": ""}, "CHATWOOT_URL"),
        ({"CHATWOOT_URL": "chat.example.com"}, "CHATWOOT_URL"),
        ({"CHATWOOT_PUBLIC_URL": "ftp://x"}, "CHATWOOT_PUBLIC_URL"),
        ({"CHATWOOT_ACCOUNT_ID": ""}, "CHATWOOT_ACCOUNT_ID is required"),
        ({"CHATWOOT_ACCOUNT_ID": "0"}, "CHATWOOT_ACCOUNT_ID"),
        ({"CHATWOOT_ACCOUNT_ID": "1.5"}, "CHATWOOT_ACCOUNT_ID"),
        ({"CHATWOOT_MAX_ITEMS": "-1"}, "CHATWOOT_MAX_ITEMS"),
        ({"CHATWOOT_TIMEOUT_SECONDS": "nan"}, "CHATWOOT_TIMEOUT_SECONDS"),
        ({"CHATWOOT_TIMEOUT_SECONDS": "soon"}, "not a number"),
        ({"MCP_HTTP_PORT": "70000"}, "MCP_HTTP_PORT"),
    ],
)
def test_bad_values_stop_the_server_at_start_up(overrides, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env({**BASE_ENV, **overrides})


@pytest.mark.parametrize(("raw", "expected"), [("true ", True), (" ON", True), ("1", True), ("no", False), ("", False)])
def test_flags_are_stripped_so_a_trailing_space_never_re_enables_writes(raw, expected):
    assert parse_flag(raw) is expected


def test_a_blank_flag_keeps_its_default():
    assert parse_flag("  ", default=True) is True
    assert module_flag("help_center") == "CHATWOOT_ENABLE_HELP_CENTER"


def test_http_mode_refuses_a_shared_key_without_the_explicit_opt_in():
    with pytest.raises(ConfigError, match="CHATWOOT_HTTP_TOKEN is set in http mode"):
        Settings.from_env({**BASE_ENV, "CHATWOOT_HTTP_TOKEN": "k"}).check_http()
    with pytest.raises(ConfigError, match="needs CHATWOOT_HTTP_TOKEN"):
        Settings.from_env({**BASE_ENV, "CHATWOOT_HTTP_SHARED_TOKEN_ROUTE": "true"}).check_http()
    Settings.from_env({**BASE_ENV}).check_http()
    Settings.from_env({**BASE_ENV, "CHATWOOT_HTTP_TOKEN": "k", "CHATWOOT_HTTP_SHARED_TOKEN_ROUTE": "true"}).check_http()
