"""Community mode: one operator-supplied key from ``CHATWOOT_HTTP_TOKEN``.

Used by ``stdio`` (one person, their own key) and, only with ``CHATWOOT_HTTP_SHARED_TOKEN_ROUTE=true``, by ``/mcp``
in http mode. The key's tier is read once (first use, or the lifespan warm-up) and kept for the life of the process.
It only hides tools when ``CHATWOOT_FILTER_TOOLS_BY_ROLE`` is on.
"""

from __future__ import annotations

from chatwoot_mcp.client import ChatwootClient
from chatwoot_mcp.client.errors import MissingTokenError
from chatwoot_mcp.credentials.base import ChatwootCredential, ProfileMemo, fetch_profile, token_identity


class StaticCredentialProvider:
    def __init__(
        self, client: ChatwootClient, token: str | None, account_id: int, *, memo: ProfileMemo | None = None
    ) -> None:
        self._client = client
        self._token = token
        self._account_id = account_id
        self._memo = memo if memo is not None else ProfileMemo(ttl_seconds=None)

    async def resolve(self) -> ChatwootCredential:
        if not self._token:
            raise MissingTokenError("no Chatwoot key is configured for this request (set CHATWOOT_HTTP_TOKEN)")
        token = self._token
        profile = await self._memo.get(
            token_identity(token), lambda: fetch_profile(self._client, token, self._account_id)
        )
        return ChatwootCredential.for_token(token, profile)

    async def warm(self) -> None:
        if self._token:
            await self.resolve()
