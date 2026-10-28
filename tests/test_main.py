import argparse

import pytest

from chatwoot_mcp.__main__ import DEFAULT_HOST, DEFAULT_PORT, http_address, main, parse_args
from chatwoot_mcp.config import ConfigError

GOOD = {"CHATWOOT_BASE_URL": "https://chat.example.com", "CHATWOOT_ACCOUNT_ID": "1"}


def test_a_mode_is_required(capsys):
    with pytest.raises(SystemExit):
        parse_args([])


def test_http_address_defaults_to_this_machine_only():
    args = parse_args(["http"])
    assert http_address(args, {}) == (DEFAULT_HOST, DEFAULT_PORT)
    assert DEFAULT_HOST == "127.0.0.1"


def test_http_address_can_come_from_the_environment_or_flags():
    assert http_address(parse_args(["http"]), {"MCP_HTTP_HOST": "0.0.0.0", "MCP_HTTP_PORT": "9000"}) == (
        "0.0.0.0",
        9000,
    )
    args = parse_args(["http", "--host", "10.0.0.5", "--port", "8123"])
    assert http_address(args, {"MCP_HTTP_PORT": "9000"}) == ("10.0.0.5", 8123)


@pytest.mark.parametrize("port", ["0", "70000", "abc"])
def test_http_address_rejects_bad_ports(port):
    with pytest.raises(ConfigError, match="port"):
        http_address(argparse.Namespace(host=None, port=None), {"MCP_HTTP_PORT": port})


def test_stdio_without_a_key_stops_with_a_plain_message(capsys):
    assert main(["stdio"], GOOD) == 2
    assert "CHATWOOT_API_TOKEN is required" in capsys.readouterr().err


def test_bad_settings_stop_the_start_with_a_plain_message(capsys):
    assert (
        main(["stdio"], {"CHATWOOT_API_TOKEN": "k", "CHATWOOT_ACCOUNT_ID": "x", "CHATWOOT_BASE_URL": "https://a.b"})
        == 2
    )
    assert "CHATWOOT_ACCOUNT_ID" in capsys.readouterr().err


def test_the_key_is_never_printed_on_a_config_error(capsys):
    main(
        ["stdio"],
        {"CHATWOOT_API_TOKEN": "super-secret-key-123", "CHATWOOT_BASE_URL": "nope", "CHATWOOT_ACCOUNT_ID": "1"},
    )
    captured = capsys.readouterr()
    assert "super-secret-key-123" not in captured.err + captured.out


def test_stdio_starts_the_server_with_the_key(monkeypatch):
    started = {}

    class Fake:
        def run(self, **kwargs):
            started.update(kwargs)

    monkeypatch.setattr(
        "chatwoot_mcp.__main__.build_server",
        lambda settings, token_getter: started.update(token=token_getter()) or Fake(),
    )
    assert main(["stdio"], {**GOOD, "CHATWOOT_API_TOKEN": " k-123456 "}) == 0
    assert started == {"token": "k-123456", "transport": "stdio", "show_banner": False}


def test_request_urls_are_not_logged_because_they_can_hold_customer_details(monkeypatch):
    import logging

    monkeypatch.setattr(
        "chatwoot_mcp.__main__.build_server",
        lambda settings, token_getter: type("S", (), {"run": lambda self, **kw: None})(),
    )
    logging.getLogger("httpx").setLevel(logging.INFO)
    main(["stdio"], {**GOOD, "CHATWOOT_API_TOKEN": "k-123456"})
    assert logging.getLogger("httpx").level == logging.WARNING
