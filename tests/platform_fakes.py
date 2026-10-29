"""A fake SSO-mode Chatwoot for platform tests, modelled on the fork's ``Sso::ProxyLoginController``,
``DeviseOverrides::SessionsController`` and ``/api/v1/profile``."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote

import httpx

from tests.conftest import ACCOUNT_ID, RecordingTransport, profile


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
