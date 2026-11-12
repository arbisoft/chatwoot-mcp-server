import dataclasses

import httpx
import pytest
from starlette.testclient import TestClient

from chatwoot_mcp import __main__ as cli
from chatwoot_mcp.config import ConfigError
from chatwoot_mcp.server import (
    MAX_ARGUMENT_CHARS,
    MAX_BODY_BYTES,
    MAX_ID,
    BodyLimit,
    argument_problem,
    build_http_app,
    build_server,
)
from tests.conftest import FakeChatwoot, rpc, tool_names, tool_text

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


def test_request_urls_are_not_logged(monkeypatch):
    import logging

    monkeypatch.setattr("os.environ", {"MCP_LOG_LEVEL": "debug"})
    cli.configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("hi\x00there", "content contains control characters"),
        ("ring\x07", "content contains control characters"),
        ("x" * (MAX_ARGUMENT_CHARS + 1), "content is longer than"),
        (MAX_ID + 1, "content is out of range"),
        ({"order\x1b": "1"}, "a key in content contains control characters"),
        (["ok", "bad\x7f"], "content[1] contains control characters"),
    ],
)
def test_the_argument_boundary_refuses_what_no_tool_should_receive(value, problem):
    assert problem in argument_problem(value, "content")


@pytest.mark.parametrize("value", ["line one\nline two\r\n\tindented", "Thanks 👨‍👩‍👧 مرحبا", MAX_ID, True, None, 1.5])
def test_the_argument_boundary_lets_ordinary_text_and_numbers_through(value):
    assert argument_problem(value, "content") is None


async def test_a_refused_argument_never_reaches_chatwoot(settings, fake):
    from fastmcp import Client

    async with Client(build_server(settings, transport=fake)) as client:
        result = await client.call_tool(
            "add_private_note", {"conversation_id": 12, "content": "pay here\x00"}, raise_on_error=False
        )
    assert result.is_error and "control characters" in result.content[0].text
    assert fake.writes() == []


def test_an_oversized_request_body_is_refused_before_it_is_read(api_key_settings, fake):
    with TestClient(app_for(api_key_settings, fake)) as client:
        big = client.post(
            API_KEY_PATH, content=b"x" * (MAX_BODY_BYTES + 1), headers={"Content-Type": "application/json"}
        )
        bad_length = client.post(API_KEY_PATH, content=b"{}", headers={"Content-Length": "abc"})
    assert big.status_code == 413 and bad_length.status_code == 413
    assert fake.requests == []


async def test_a_chunked_body_that_grows_past_the_limit_reads_as_a_disconnect():
    chunks = iter(
        [{"type": "http.request", "body": b"x" * 6, "more_body": True}, {"type": "http.request", "body": b"x" * 6}]
    )
    seen = []

    async def app(_scope, receive, _send):
        seen.extend([(await receive())["type"], (await receive())["type"]])

    async def receive():
        return next(chunks)

    await BodyLimit(app, max_bytes=10)({"type": "http", "headers": []}, receive, None)
    assert seen == ["http.request", "http.disconnect"]


async def test_an_id_sent_as_a_huge_numeric_string_is_still_refused(settings, fake):
    # The argument boundary sees a short string; the schema's upper bound catches it after pydantic's coercion.
    from fastmcp import Client

    async with Client(build_server(settings, transport=fake)) as client:
        result = await client.call_tool("get_conversation", {"conversation_id": "99999999999"}, raise_on_error=False)
    assert result.is_error and "less_than_equal" in result.content[0].text
    assert [r for r in fake.requests if "/conversations" in r.url.path] == []


async def test_a_failed_tool_is_an_mcp_error_with_the_same_text_and_one_log_line(settings, fake, caplog):
    import logging

    from fastmcp import Client

    fake.on("GET", "/conversations/12", status=404, json={"error": "missing"})
    fake.on("GET", "/conversations/13", json={"id": 13})
    with caplog.at_level(logging.WARNING, logger="chatwoot_mcp.server"):
        async with Client(build_server(settings, transport=fake)) as client:
            failed = await client.call_tool("get_conversation", {"conversation_id": 12}, raise_on_error=False)
            worked = await client.call_tool("get_conversation", {"conversation_id": 13}, raise_on_error=False)
    assert failed.is_error and failed.content[0].text.startswith("Error: not found (HTTP 404)")
    assert not worked.is_error
    lines = [r.getMessage() for r in caplog.records if r.name == "chatwoot_mcp.server"]
    # The tool and the error are logged; the arguments never are.
    assert len(lines) == 1 and lines[0].startswith("tool get_conversation failed: Error: not found (HTTP 404)")


async def test_a_slow_read_is_stopped_at_the_deadline_but_a_write_is_not(settings, fake):
    import asyncio
    import dataclasses

    from fastmcp import Client

    async def slow(_request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, json={"id": 12, "payload": {"current_status": "pending"}})

    fake.on("GET", "/conversations/12", handler=slow)
    fake.on("POST", "/conversations/12/toggle_status", handler=slow)
    quick = dataclasses.replace(settings, tool_timeout_seconds=0.1)
    async with Client(build_server(quick, transport=fake)) as client:
        read = await client.call_tool("get_conversation", {"conversation_id": 12}, raise_on_error=False)
        write = await client.call_tool(
            "set_conversation_status", {"conversation_id": 12, "status": "pending"}, raise_on_error=False
        )
    assert read.is_error and "did not answer within 0.1 seconds" in read.content[0].text
    # A write cut short would have an unknown outcome, so it is never stopped by the deadline.
    assert not write.is_error and write.data["status"] == "pending"
