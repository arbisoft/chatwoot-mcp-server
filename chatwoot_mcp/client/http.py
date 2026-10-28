"""The only code that talks to Chatwoot.

Three guarantees, each covered by a test:
- the key is sent in Chatwoot's own `api_access_token` header and nowhere else, and never shown in an error;
- no cookies and no `X-Auth-Request-*` headers are ever sent (those carry identity inside Moneta and
  must come from the front door, never from here);
- redirects are not followed, so a key cannot be handed to another host.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import httpx

from chatwoot_mcp.client.errors import ChatwootError, error_for_status
from chatwoot_mcp.config import Settings

TokenGetter = Callable[[], str | None]


def _query(params: Mapping[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out[f"{key}[]"] = [str(v) for v in value]
        else:
            out[key] = value
    return out


class ChatwootClient:
    def __init__(
        self,
        settings: Settings,
        *,
        token_getter: TokenGetter,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._settings = settings
        self._token_getter = token_getter
        self._transport = transport

    def _account_path(self, path: str, version: int) -> str:
        return f"/api/v{version}/accounts/{self._settings.account_id}{path}"

    async def get_profile(self) -> Any:
        return await self._request("GET", "/api/v1/profile")

    async def get_account(self, path: str, *, params: Mapping[str, Any] | None = None, version: int = 1) -> Any:
        return await self._request("GET", self._account_path(path, version), params=params)

    async def post_account(self, path: str, body: Mapping[str, Any], *, version: int = 1) -> Any:
        return await self._request("POST", self._account_path(path, version), json=dict(body))

    async def _request(
        self, method: str, path: str, *, params: Mapping[str, Any] | None = None, json: Any = None
    ) -> Any:
        token = self._token_getter()
        if not token:
            raise ChatwootError("No Chatwoot key was provided for this request.")
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.base_url,
                timeout=self._settings.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as http:
                response = await http.request(
                    method, path, params=_query(params), json=json, headers={"api_access_token": token}
                )
        except httpx.TimeoutException:
            raise ChatwootError("Chatwoot took too long to answer.") from None
        except httpx.HTTPError:
            raise ChatwootError("The server could not reach Chatwoot.") from None
        try:
            return self._parse(response)
        except ChatwootError as error:
            # Chatwoot's own reason text is shown to the user, so make sure it can never carry the key.
            message = str(error).replace(token, "[key hidden]") if len(token) >= 8 else str(error)
            raise ChatwootError(message, error.status) from None

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        if response.status_code >= 300:
            try:
                body = response.json()
            except ValueError:
                body = None
            raise error_for_status(response.status_code, body)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ChatwootError("Chatwoot answered with something that is not JSON.") from None
