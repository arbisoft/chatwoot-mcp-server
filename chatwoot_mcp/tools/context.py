"""What every tool module needs, and the per-call Chatwoot session.

Tools call ``await context.session()`` once, then talk to Chatwoot through the returned :class:`ChatwootSession`,
which carries the caller's key and tier and puts every path under the configured account. Tools never take an
account id, so the assistant cannot move to another account.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any

from fastmcp.server.auth import AuthCheck
from fastmcp.server.dependencies import get_context
from fastmcp.server.elicitation import AcceptedElicitation
from mcp.types import ClientCapabilities, ElicitationCapability, ToolAnnotations
from pydantic import Field

from chatwoot_mcp.client import ChatwootClient, ChatwootError, to_tool_error
from chatwoot_mcp.client.http import accepted_but_unreadable, never_sent, outcome_unknown
from chatwoot_mcp.config import Settings
from chatwoot_mcp.confirmations import ConfirmationError, Confirmations
from chatwoot_mcp.credentials import ChatwootCredential, CredentialProvider
from chatwoot_mcp.shaping import Links
from chatwoot_mcp.tiers import Tier, tier_allows

# Chatwoot's ids are 32-bit. Bounding every id here also catches "99999999999" sent as a string, which pydantic turns
# into an int only after the argument boundary has looked at it.
MAX_ID = 2_147_483_647
ConversationId = Annotated[int, Field(ge=1, le=MAX_ID, description="The conversation number shown in Chatwoot.")]
ContactId = Annotated[
    int, Field(ge=1, le=MAX_ID, description="The customer's id in Chatwoot. Use search_contacts to find it.")
]
Page = Annotated[int, Field(ge=1, le=MAX_ID, description="Page number, starting at 1.")]
ConfirmationToken = Annotated[str, Field(min_length=1, max_length=200, description="From the prepare tool.")]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatwootSession:
    client: ChatwootClient
    credential: ChatwootCredential
    account_id: int

    @property
    def tier(self) -> Tier | None:
        return self.credential.tier

    def lacks(self, tier: Tier) -> bool:
        return self.tier is None or not tier_allows(self.tier, tier)

    def _path(self, path: str, version: int) -> str:
        return f"/api/v{version}/accounts/{self.account_id}{path}"

    async def get(
        self, path: str, params: Mapping[str, Any] | None = None, *, version: int = 1, refusal_expected: bool = False
    ) -> Any:
        """``refusal_expected``: the caller falls back on a role refusal, so it must not schedule a re-check."""
        return await self.client.get(
            self._path(path, version), token=self.credential.token, params=params, refusal_expected=refusal_expected
        )

    async def search(self, path: str, body: Mapping[str, Any]) -> Any:
        """A read-only POST, such as ``/conversations/filter``: safe to retry."""
        return await self.client.request(
            "POST", self._path(path, 1), token=self.credential.token, json=dict(body), retry_safe=True
        )

    async def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return await self.client.request("POST", self._path(path, 1), token=self.credential.token, json=dict(body))

    async def put(self, path: str, body: Mapping[str, Any]) -> Any:
        """PUT is retried on timeouts, so it must only ever set fields, never send a message."""
        return await self.client.request("PUT", self._path(path, 1), token=self.credential.token, json=dict(body))


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def send_failure(error: ChatwootError, what: str, *, done: str = "sent") -> str:
    """After a failed write that is never retried: say whether it happened (surely not, maybe, or yes)."""
    if accepted_but_unreadable(error):
        return (
            f"Error: Chatwoot accepted the {what}, so it was {done}, but its answer could not be read; check Chatwoot"
        )
    if never_sent(error):
        return f"{to_tool_error(error)}; nothing was {done}, try again"
    if outcome_unknown(error):
        return f"{to_tool_error(error)}; the {what} may or may not have been {done}. Check Chatwoot before trying again"
    return to_tool_error(error)


def limited(items: list[Any], maximum: int) -> tuple[list[Any], bool]:
    """The first ``maximum`` items, and whether any were cut."""
    return items[:maximum], len(items) > maximum


def local_page(items: list[Any], page: int, size: int) -> tuple[list[Any], dict[str, Any]]:
    """One page of a list Chatwoot returns whole, and the page fields to return with it."""
    start = (page - 1) * size
    info: dict[str, Any] = {"page": page}
    if len(items) > start + size:
        info["next_page"] = page + 1
    return items[start : start + size], info


def page_info(page: int, cut: bool, maximum: int) -> dict[str, Any]:
    info: dict[str, Any] = {"page": page}
    if cut:
        info["truncated"] = f"Only the first {maximum} items are shown. Narrow the search to see the rest."
    return info


UNKNOWN_ROLE = "your Chatwoot role could not be determined; try again in a minute"


def tier_error(needed: Tier, actual: Tier | None) -> str:
    account = f"your account is {actual.label}" if actual is not None else UNKNOWN_ROLE
    return f"Error: this tool needs a Chatwoot {needed.name.lower()}; {account}"


CONFIRMATION_STORE_DOWN = "Error: the confirmation store is unavailable, so nothing was changed; try again shortly"


def next_step(tool: str) -> str:
    return (
        f"Show this preview to the user. Only after they clearly agree, call {tool} with confirmation_token and "
        "exactly the same arguments. Do not call it without their agreement."
    )


async def prepared(
    context: ToolContext, session: ChatwootSession, *, action: str, payload: Any, preview: dict[str, Any]
) -> dict[str, Any] | str:
    """Issue a confirmation for ``payload`` and return the preview with its token, or an ``Error:`` string."""
    try:
        token = await context.confirmations.issue(identity=session.credential.identity, action=action, payload=payload)
    except ConfirmationError as error:
        return f"Error: {error}"
    except Exception:
        logger.warning("could not store a confirmation", exc_info=True)
        return CONFIRMATION_STORE_DOWN
    return {
        "confirmation_token": token,
        "expires_in_seconds": context.confirmations.ttl_seconds,
        "preview": preview,
        "next_step": next_step(action),
    }


async def confirmation_refusal(
    context: ToolContext, session: ChatwootSession, token: str, *, action: str, payload: Any
) -> str | None:
    """Spend ``token`` for this exact request; ``None`` when it may go ahead, else an ``Error:`` string."""
    try:
        await context.confirmations.consume(token, identity=session.credential.identity, action=action, payload=payload)
    except ConfirmationError as error:
        return f"Error: {error}"
    except Exception:
        logger.warning("could not read a confirmation", exc_info=True)
        return CONFIRMATION_STORE_DOWN
    return await human_refusal(action, payload)


APPROVE = "approve"
APPROVAL_TEXT_CHARS = 4000
NOT_APPROVED = "Error: the user did not approve this in the approval prompt; nothing was changed"


async def human_refusal(action: str, payload: Any) -> str | None:
    """Ask the person directly when the client can show an approval prompt (MCP elicitation).

    The model holds the confirmation token, so on its own the token proves only that a preview was made; the prompt
    goes to the person, and the model cannot answer it. Clients without elicitation (and stateless HTTP, where the
    server keeps no client capabilities) keep the token-only step. ``None`` means approved or not askable.
    """
    try:
        ctx = get_context()
    except RuntimeError:
        return None
    if not ctx.session.check_client_capability(ClientCapabilities(elicitation=ElicitationCapability())):
        return None
    details = json.dumps(payload, ensure_ascii=False, indent=2, default=str)[:APPROVAL_TEXT_CHARS]
    message = f"Approve: {action.replace('_', ' ')} in Chatwoot?\n{details}"
    try:
        answer = await ctx.elicit(message, response_type=[APPROVE, "cancel"])
    except Exception:
        logger.warning("could not ask the user to approve %s", action, exc_info=True)
        return NOT_APPROVED
    if isinstance(answer, AcceptedElicitation) and answer.data == APPROVE:
        return None
    return NOT_APPROVED


@dataclass(frozen=True)
class ToolSpec:
    """Annotation presets; every tool picks one so all four hints are always set."""

    read_only: bool
    destructive: bool
    idempotent: bool

    def annotations(self, title: str) -> ToolAnnotations:
        return ToolAnnotations(
            title=title,
            readOnlyHint=self.read_only,
            destructiveHint=self.destructive,
            idempotentHint=self.idempotent,
            openWorldHint=True,
        )


READ = ToolSpec(read_only=True, destructive=False, idempotent=True)
CREATE = ToolSpec(read_only=False, destructive=False, idempotent=False)
ADD = ToolSpec(read_only=False, destructive=False, idempotent=True)
OVERWRITE = ToolSpec(read_only=False, destructive=True, idempotent=True)
PREPARE = ToolSpec(read_only=False, destructive=False, idempotent=False)
IRREVERSIBLE = ToolSpec(read_only=False, destructive=True, idempotent=False)


@dataclass(frozen=True)
class ToolContext:
    client: ChatwootClient
    credentials: CredentialProvider
    settings: Settings
    links: Links
    tier_check: AuthCheck
    confirmations: Confirmations

    async def session(self) -> ChatwootSession:
        return ChatwootSession(self.client, await self.credentials.resolve(), self.settings.account_id)

    def tool(self, title: str, spec: ToolSpec, *, module: str, tier: Tier) -> dict[str, Any]:
        """Keyword arguments for ``@mcp.tool``: annotations, tier and module tags, and the tier check."""
        kind = "read" if spec.read_only else "write"
        return {
            "annotations": spec.annotations(title),
            "tags": {tier.tag, f"module:{module}", kind},
            "auth": self.tier_check,
        }

    @property
    def writes_enabled(self) -> bool:
        return not self.settings.read_only

    @property
    def max_items(self) -> int:
        return self.settings.max_items

    @property
    def max_chars(self) -> int:
        return self.settings.max_text_chars
