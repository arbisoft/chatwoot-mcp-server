import dataclasses

import pytest
from starlette.testclient import TestClient

from chatwoot_mcp import __main__ as cli
from chatwoot_mcp.config import ConfigError
from chatwoot_mcp.server import build_http_app, build_server
from tests.conftest import FakeChatwoot
from tests.helpers import rpc, tool_names, tool_text

API_KEY_PATH = "/http/api-key/mcp"


@pytest.fixture
def api_key_settings(settings):
    return dataclasses.replace(settings, http_token=None)


def app_for(settings, fake):
    return build_http_app(build_server(settings, transport=fake), settings)


def test_healthz_and_the_tool_list_need_no_key(api_key_settings, fake):
    with TestClient(app_for(api_key_settings, fake)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert "get_me" in tool_names(rpc(client, API_KEY_PATH, "tools/list", {}))


def test_each_caller_uses_their_own_key_and_identity_headers_never_get_through(api_key_settings, fake):
    with TestClient(app_for(api_key_settings, fake)) as client:
        for key in ("ada-key", "bob-key"):
            headers = {"X-Chatwoot-Token": key, "X-Auth-Request-Email": "boss@example.com", "x_auth_request_user": "x"}
            rpc(client, API_KEY_PATH, "tools/call", {"name": "get_me", "arguments": {}}, headers=headers)
    used = [r.headers["api_access_token"] for r in fake.requests]
    assert list(dict.fromkeys(used)) == ["ada-key", "bob-key"]
    for request in fake.requests:
        assert not any(name.lower().startswith("x-auth-request") for name in request.headers)


def test_a_call_without_a_key_is_a_plain_error(api_key_settings, fake):
    with TestClient(app_for(api_key_settings, fake)) as client:
        result = rpc(client, API_KEY_PATH, "tools/call", {"name": "get_me", "arguments": {}})
    assert "X-Chatwoot-Token" in tool_text(result)


def test_mcp_is_not_served_with_the_shared_key_unless_asked(api_key_settings, settings, fake):
    with TestClient(app_for(api_key_settings, fake)) as client:
        assert client.post("/mcp", json={}).status_code == 404
    with pytest.raises(ConfigError, match="CHATWOOT_HTTP_TOKEN is set in http mode"):
        app_for(settings, fake)
    shared = dataclasses.replace(settings, http_shared_token_route=True)
    with TestClient(app_for(shared, fake)) as client:
        rpc(client, "/mcp", "tools/call", {"name": "get_me", "arguments": {}})
    assert fake.requests[-1].headers["api_access_token"] == settings.http_token


def test_a_chatwoot_that_is_down_never_stops_the_server_starting(settings):
    broken = FakeChatwoot()
    broken.on_path("GET", "/api/v1/profile", status=500, json={})
    shared = dataclasses.replace(settings, http_shared_token_route=True)
    with TestClient(app_for(shared, broken)) as client:
        assert client.get("/healthz").status_code == 200


ENV = {"CHATWOOT_URL": "https://chat.test", "CHATWOOT_ACCOUNT_ID": "5"}


def test_main_runs_stdio_by_default(monkeypatch):
    ran = []
    monkeypatch.setattr("os.environ", {**ENV, "CHATWOOT_HTTP_TOKEN": "k"})
    monkeypatch.setattr("fastmcp.FastMCP.run", lambda self, **kwargs: ran.append(kwargs))
    cli.main([])
    assert ran == [{"show_banner": False}]


def test_main_serves_http(monkeypatch):
    served = {}
    monkeypatch.setattr("os.environ", {**ENV, "MCP_HTTP_PORT": "9001"})
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: served.update(kwargs))
    cli.main(["http"])
    assert served["port"] == 9001 and served["host"] == "127.0.0.1"


@pytest.mark.parametrize(
    ("env", "argv"),
    [
        ({"CHATWOOT_ACCOUNT_ID": "5"}, []),
        ({**ENV, "CHATWOOT_HTTP_TOKEN": "k"}, ["http"]),
    ],
)
def test_main_exits_with_a_plain_message_on_a_bad_setup(monkeypatch, env, argv):
    monkeypatch.setattr("os.environ", env)
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert str(raised.value).startswith("chatwoot-mcp: ")


def test_main_hands_http_to_platform_mode_when_cognito_is_set(monkeypatch):
    handed = []
    monkeypatch.setattr("os.environ", {"COGNITO_USER_POOL_ID": "pool"})
    monkeypatch.setattr(cli.platform, "run", lambda: handed.append(True))
    cli.main(["http"])
    assert handed == [True]


def test_request_urls_are_not_logged(monkeypatch):
    import logging

    monkeypatch.setattr("os.environ", {"MCP_LOG_LEVEL": "debug"})
    cli.configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
