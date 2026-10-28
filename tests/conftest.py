from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastmcp import Client

from chatwoot_mcp.config import Settings
from chatwoot_mcp.server import build_server

Handler = Callable[[httpx.Request], httpx.Response]

BASE = "https://chat.test"
ACCOUNT = "/api/v1/accounts/5"


class FakeChatwoot:
    """A pretend Chatwoot: routes (method, path) to canned JSON and remembers every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.requests: list[httpx.Request] = []

    def add(self, method: str, path: str, body: Any, status: int = 200) -> None:
        self.routes[(method, path)] = (status, body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        found = self.routes.get((request.method, request.url.path))
        if found is None:
            return httpx.Response(404, json={"error": f"no route {request.method} {request.url.path}"})
        status, body = found
        return httpx.Response(status, json=body)

    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_json(self) -> Any:
        return json.loads(self.last().read())


@pytest.fixture
def chatwoot() -> FakeChatwoot:
    return FakeChatwoot()


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def build(**overrides: str) -> Settings:
        env = {"CHATWOOT_BASE_URL": BASE, "CHATWOOT_ACCOUNT_ID": "5", **overrides}
        return Settings.from_env(env)

    return build


@pytest.fixture
def make_client(chatwoot, make_settings):
    """An in-memory MCP client wired to the pretend Chatwoot, acting as a user with key 'user-key'."""

    def build(token: str | None = "user-key", **overrides: str) -> Client:
        server = build_server(
            make_settings(**overrides),
            token_getter=lambda: token,
            transport=httpx.MockTransport(chatwoot),
        )
        return Client(server)

    return build


async def call(client: Client, tool: str, /, **arguments: Any) -> Any:
    """Call a tool; returns the structured result (a dict or list)."""
    async with client:
        result = await client.call_tool(tool, arguments)
    return result.data if result.data is not None else json.loads(result.content[0].text)


async def tool_names(client: Client) -> set[str]:
    async with client:
        return {tool.name for tool in await client.list_tools()}
