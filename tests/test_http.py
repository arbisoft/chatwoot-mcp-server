import json
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from chatwoot_mcp.credentials.header import is_identity_header, request_token
from chatwoot_mcp.server import build_http_app, build_server

PATH = "/http/api-key/mcp"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ACCOUNT = "/api/v1/accounts/5"


@pytest.fixture
def app(chatwoot, make_settings):
    server = build_server(make_settings(), token_getter=request_token, transport=httpx.MockTransport(chatwoot))
    return build_http_app(server)


def rpc(
    client: TestClient, method: str, params: dict[str, Any], headers: dict[str, str] | None = None, path: str = PATH
):
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = client.post(path, json=message, headers={**MCP_HEADERS, **(headers or {})})
    assert response.status_code == 200, response.text
    lines = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    return json.loads(lines[-1] if lines else response.text)["result"]


def call_tool(client, name, arguments=None, headers=None):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, headers)


def test_health_check_needs_no_key(app):
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"status": "ok"}


def test_the_tool_list_is_served_without_a_key(app):
    with TestClient(app) as client:
        result = rpc(client, "tools/list", {})
    assert len(result["tools"]) == 24


def test_each_caller_acts_with_their_own_key(app, chatwoot):
    chatwoot.add("GET", f"{ACCOUNT}/teams", [])
    with TestClient(app) as client:
        call_tool(client, "list_teams", headers={"X-Chatwoot-Token": "alice-key"})
        assert chatwoot.last().headers["api_access_token"] == "alice-key"
        call_tool(client, "list_teams", headers={"X-Chatwoot-Token": "bob-key"})
        assert chatwoot.last().headers["api_access_token"] == "bob-key"


def test_a_missing_key_is_a_plain_tool_error_and_nothing_is_sent(app, chatwoot):
    with TestClient(app) as client:
        result = call_tool(client, "list_teams")
    assert result["isError"] is True
    assert "No Chatwoot key" in result["content"][0]["text"]
    assert chatwoot.requests == []


def test_a_blank_key_counts_as_missing(app, chatwoot):
    with TestClient(app) as client:
        result = call_tool(client, "list_teams", headers={"X-Chatwoot-Token": "   "})
    assert result["isError"] is True and chatwoot.requests == []


def test_identity_headers_from_the_caller_never_reach_chatwoot(app, chatwoot):
    chatwoot.add("GET", f"{ACCOUNT}/teams", [])
    spoof = {
        "X-Chatwoot-Token": "alice-key",
        "X-Auth-Request-Email": "admin@example.com",
        "X_Auth_Request_Email": "admin@example.com",
        "X-Auth-Request-User": "admin",
    }
    with TestClient(app) as client:
        call_tool(client, "list_teams", headers=spoof)
    sent = {name.lower().replace("_", "-") for name in chatwoot.last().headers}
    assert not [name for name in sent if name.startswith("x-auth-request")]
    assert chatwoot.last().headers["api_access_token"] == "alice-key"


def test_a_caller_cannot_choose_chatwoots_own_header_directly(app, chatwoot):
    chatwoot.add("GET", f"{ACCOUNT}/teams", [])
    with TestClient(app) as client:
        call_tool(client, "list_teams", headers={"X-Chatwoot-Token": "mine-key", "api_access_token": "someone-elses"})
    assert chatwoot.last().headers["api_access_token"] == "mine-key"


def test_the_plain_mcp_path_is_not_served_in_this_mode(app):
    with TestClient(app) as client:
        response = client.post("/mcp", json={}, headers=MCP_HEADERS)
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("X-Auth-Request-Email", True),
        ("x-auth-request-user", True),
        ("X_Auth_Request_Access_Token", True),
        ("X-Chatwoot-Token", False),
        ("Authorization", False),
        ("X-Auth-Requested", False),
    ],
)
def test_which_headers_count_as_identity_headers(name, expected):
    assert is_identity_header(name) is expected


async def test_the_mount_itself_strips_identity_headers_before_anything_else_sees_them():
    from chatwoot_mcp.credentials.header import api_key_mount

    seen = {}

    async def inner(scope, receive, send):
        seen["headers"] = [name.decode() for name, _ in scope["headers"]]

    scope = {
        "type": "http",
        "headers": [(b"x-auth-request-email", b"a@b.c"), (b"x_auth_request_user", b"u"), (b"x-chatwoot-token", b"k")],
    }
    await api_key_mount(inner)(scope, None, None)
    assert seen["headers"] == ["x-chatwoot-token"]
