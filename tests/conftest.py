from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastmcp import Client
from starlette.testclient import TestClient

from chatwoot_mcp.config import Settings
from chatwoot_mcp.platform.cognito import IdTokenVerifier
from chatwoot_mcp.server import build_server

if TYPE_CHECKING:
    import httpx

    from chatwoot_mcp.config import Settings
import secrets
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote

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


MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(client: TestClient, path: str, method: str, params: dict[str, Any], headers: dict[str, str] | None = None):
    """One JSON-RPC call against the stateless MCP endpoint; returns the decoded ``result``."""
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = client.post(path, json=message, headers={**MCP_HEADERS, **(headers or {})})
    assert response.status_code == 200, response.text
    data_lines = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    payload = json.loads(data_lines[-1]) if data_lines else response.json()
    return payload["result"]


def tool_names(result: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in result["tools"]}


def tool_text(result: dict[str, Any]) -> str:
    return result["content"][0]["text"]


_FRAME = re.compile(r'^<untrusted_content source="[a-z_]+" id="[^"]*">\n?(.*?)\n?</untrusted_content>$', re.DOTALL)


def unframed(value: Any) -> Any:
    """Strip ``<untrusted_content>`` frames recursively, for comparing shaped output with plain values."""
    if isinstance(value, dict):
        return {unframed(key): unframed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unframed(item) for item in value]
    if isinstance(value, str):
        match = _FRAME.match(value)
        return match.group(1) if match else value
    return value


async def tools(settings: Settings, fake: httpx.AsyncBaseTransport) -> dict[str, Any]:
    """The registered tools by name, as an MCP client lists them."""
    async with Client(build_server(settings, transport=fake)) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def call(settings: Settings, fake: httpx.AsyncBaseTransport, name: str, arguments: dict[str, Any] | None = None):
    async with Client(build_server(settings, transport=fake)) as client:
        return await client.call_tool(name, arguments or {})


async def data(
    settings: Settings, fake: httpx.AsyncBaseTransport, name: str, arguments: dict[str, Any] | None = None
) -> Any:
    """The tool's structured result, or its ``Error: ...`` string."""
    result = await call(settings, fake, name, arguments)
    if result.data is not None:
        return result.data
    return result.content[0].text


@dataclass
class FakeUser:
    role: str = "agent"
    key: str = field(default_factory=lambda: f"key-{secrets.token_hex(4)}")
    member: bool = True
    corporate: bool = True


class FakeSsoChatwoot(RecordingTransport):
    def __init__(self, *, sso_mode: bool = True, corporate_gate: bool = False) -> None:
        self.users: dict[str, FakeUser] = {}
        self.handoffs: dict[str, str] = {}
        self.sessions: dict[str, str] = {}
        self.signed_out: list[str] = []
        self.created_users: list[str] = []
        self.sso_mode = sso_mode
        self.corporate_gate = corporate_gate
        self.fail_sign_in = False
        self.throttle_sign_in = False
        self.handoff_error: str | None = None
        super().__init__(self._dispatch)

    def add(self, email: str, **options: Any) -> FakeUser:
        self.users[email] = FakeUser(**options)
        return self.users[email]

    def handoff_count(self) -> int:
        return sum(1 for r in self.requests if r.url.path == "/auth/sso/proxy-login")

    def live_sessions(self) -> list[str]:
        return [client for client in self.sessions if client not in self.signed_out]

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth/sso/proxy-login":
            return self._proxy_login(request)
        if path == "/auth/sign_in":
            return self._sign_in(request)
        if path == "/auth/sign_out":
            return self._sign_out(request)
        if path == "/api/v1/profile":
            return self._profile(request)
        return httpx.Response(404, json={"error": "not found"})

    def _proxy_login(self, request: httpx.Request) -> httpx.Response:
        if not self.sso_mode:
            return httpx.Response(404)
        email = request.headers.get("x-auth-request-email", "").strip().lower()
        if not email or self.handoff_error:
            error = self.handoff_error or "no-identity"
            return httpx.Response(302, headers={"location": f"https://chat.test/app/login?error={error}"})
        user = self.users.get(email)
        if user is None:
            self.created_users.append(email)
            user = self.add(email)
        if self.corporate_gate and (not request.headers.get("x-auth-request-access-token") or not user.corporate):
            return httpx.Response(403)
        token = secrets.token_hex(8)
        self.handoffs[token] = email
        return httpx.Response(
            302, headers={"location": f"https://chat.test/app/login?email={quote(email)}&sso_auth_token={token}"}
        )

    def _sign_in(self, request: httpx.Request) -> httpx.Response:
        if self.throttle_sign_in:
            return httpx.Response(429, json={"error": "Retry later"})
        body = json.loads(request.content)
        email = self.handoffs.pop(body.get("sso_auth_token", ""), None)
        if self.fail_sign_in or email is None or email != body.get("email") or "password" in body:
            return httpx.Response(404)
        client = secrets.token_hex(4)
        self.sessions[client] = email
        headers = {"access-token": "session-token", "client": client, "uid": email, "token-type": "Bearer"}
        return httpx.Response(
            200, json={"data": {"email": email, "access_token": self.users[email].key}}, headers=headers
        )

    def _sign_out(self, request: httpx.Request) -> httpx.Response:
        client = request.headers.get("client", "")
        if self.sessions.get(client) != request.headers.get("uid"):
            return httpx.Response(404)
        self.signed_out.append(client)
        return httpx.Response(200, json={"success": True})

    def _profile(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("api_access_token")
        user = next((u for u in self.users.values() if u.key == key), None)
        if user is None:
            return httpx.Response(401, json={"error": "Invalid Access Token"})
        return httpx.Response(200, json=profile(user.role, account_id=ACCOUNT_ID if user.member else 99))


def query_of(location: str) -> dict[str, list[str]]:
    return parse_qs(location.split("?", 1)[1])


# id_tokens in platform tests are RS256-signed like Cognito's, and verified against this key set.
_ID_TOKEN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ID_TOKEN_AUDIENCE = "chatwoot-mcp-client"
ID_TOKEN_JWKS = {
    "keys": [{**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_ID_TOKEN_KEY.public_key())), "kid": "test-key"}]
}


def signed_id_token(claims: dict[str, Any], *, audience: str = ID_TOKEN_AUDIENCE) -> str:
    return jwt.encode({"aud": audience, **claims}, _ID_TOKEN_KEY, algorithm="RS256", headers={"kid": "test-key"})


def id_token_verifier(audience: str = ID_TOKEN_AUDIENCE) -> IdTokenVerifier:
    async def fetch() -> dict[str, Any]:
        return ID_TOKEN_JWKS

    return IdTokenVerifier(fetch, audience)
