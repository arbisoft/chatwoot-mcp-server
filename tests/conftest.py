from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from chatwoot_mcp.config import Settings

Handler = Callable[[httpx.Request], httpx.Response]

BASE = "https://chat.test"
ACCOUNT_ID = 5
ACCOUNT = f"/api/v1/accounts/{ACCOUNT_ID}"
REPORTS = f"/api/v2/accounts/{ACCOUNT_ID}"
TOKEN = "secret-token"


def profile(role: str = "agent", permissions: list[str] | None = None, account_id: int = ACCOUNT_ID) -> dict[str, Any]:
    """``GET /api/v1/profile``, including the tokens the server must never echo."""
    return {
        "id": 7,
        "name": "Ada Agent",
        "email": "ada@example.com",
        "access_token": "PROFILE-ACCESS-TOKEN",
        "pubsub_token": "PROFILE-PUBSUB-TOKEN",
        "availability_status": "online",
        "accounts": [{"id": account_id, "name": "Acme", "role": role, "permissions": permissions or [role]}],
    }


AGENT_PROFILE = profile()
ADMIN_PROFILE = profile("administrator")


class RecordingTransport(httpx.MockTransport):
    def __init__(self, handler: Handler) -> None:
        self.requests: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        super().__init__(record)


Route = httpx.Response | Handler


class FakeChatwoot(RecordingTransport):
    """A routed fake: ``fake.on("GET", "/conversations/12", json={...})`` under the account; unknown routes are 404.

    ``on`` takes account-relative paths; ``on_path`` takes full ones (the profile, reports).
    """

    def __init__(self, me: dict[str, Any] | None = None) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        super().__init__(self._dispatch)
        self.on_path("GET", "/api/v1/profile", json=me or AGENT_PROFILE)

    def on_path(self, method: str, path: str, *, json: Any = None, status: int = 200, handler: Handler | None = None):
        self.routes[(method, path)] = handler or httpx.Response(status, json=json)
        return self

    def on(self, method: str, path: str, **options: Any):
        return self.on_path(method, f"{ACCOUNT}{path}", **options)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"error": f"no route {request.method} {request.url.path}"})
        if callable(route):
            return route(request)
        return httpx.Response(route.status_code, content=route.content, headers=route.headers)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == f"{ACCOUNT}{path}"]

    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET"]

    def last_json(self, method: str, path: str) -> Any:
        return json.loads(self.calls(method, path)[-1].content)


@pytest.fixture
def settings() -> Settings:
    return Settings(chatwoot_url=BASE, account_id=ACCOUNT_ID, http_token=TOKEN)


@pytest.fixture
def everything(settings) -> Settings:
    """Every module on, including the opt-in replies and macros."""
    from chatwoot_mcp.config import TOOL_MODULES

    return dataclasses.replace(settings, enabled_modules=frozenset(TOOL_MODULES))


@pytest.fixture
def read_only(everything) -> Settings:
    return dataclasses.replace(everything, read_only=True)


@pytest.fixture
def fake() -> FakeChatwoot:
    return FakeChatwoot()


@pytest.fixture
def admin_fake() -> FakeChatwoot:
    return FakeChatwoot(me=ADMIN_PROFILE)
