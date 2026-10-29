"""Two-step confirmation for actions that must not run on a model's whim.

``issue`` stores a record bound to the caller's identity, the action name and the sha256 of the exact payload, and
returns a random token. ``consume`` takes the record out atomically (single use, even when the check then fails),
and succeeds only if it has not expired and identity, action and payload all match. Records are stored under
``sha256(token)``, never the token itself.

One identity may hold at most ``max_outstanding_per_identity`` live confirmations, so no single caller can fill the
store, and the memory backend holds at most ``max_entries`` records in all.

The backend is a two-method protocol: :class:`MemoryConfirmationBackend` (one process), and in platform mode
``chatwoot_mcp.platform.storage.ValkeyConfirmationBackend`` (``SET key value EX ttl`` and ``GETDEL key``,
Fernet-encrypted), shared by every replica. The per-identity limit is kept in each process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from chatwoot_mcp.config import DEFAULT_CONFIRM_TTL_SECONDS

KEY_PREFIX = "chatwoot-mcp:confirm:v1:"
MAX_OUTSTANDING_PER_IDENTITY = 20
# ponytail: a count cap, not Zammad's byte cap. Our records hold ids and short text only; add a byte ledger if a
# confirmation ever has to carry a whole message body.
MAX_MEMORY_ENTRIES = 10_000

Clock = Callable[[], float]


class ConfirmationError(Exception):
    """The confirmation token is unknown, used, expired or does not match."""


class ConfirmationLimitError(ConfirmationError):
    """A new confirmation would exceed the caller's or the store's limit."""


class ConfirmationBackend(Protocol):
    async def put(self, key: str, value: str, ttl_seconds: int) -> None: ...

    async def take(self, key: str) -> str | None:
        """Atomically return and delete the value, or ``None`` if absent or expired."""
        ...


class MemoryConfirmationBackend:
    """No lock: no method awaits. The full-store sweep runs only when the cap is reached."""

    def __init__(self, clock: Clock = time.time, *, max_entries: int = MAX_MEMORY_ENTRIES) -> None:
        self._clock = clock
        self._max_entries = max_entries
        self._values: dict[str, tuple[str, float]] = {}

    async def put(self, key: str, value: str, ttl_seconds: int) -> None:
        now = self._clock()
        self._values.pop(key, None)
        if len(self._values) >= self._max_entries:
            self._values = {k: entry for k, entry in self._values.items() if entry[1] > now}
            if len(self._values) >= self._max_entries:
                raise ConfirmationLimitError(
                    "too many actions are waiting for confirmation on this server; try again in a few minutes"
                )
        self._values[key] = (value, now + ttl_seconds)

    async def take(self, key: str) -> str | None:
        entry = self._values.pop(key, None)
        if entry is None or entry[1] <= self._clock():
            return None
        return entry[0]


def payload_digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


def _storage_key(token: str) -> str:
    return KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class _Record:
    identity: str
    action: str
    payload_sha256: str
    expires_at: float


class Confirmations:
    def __init__(
        self,
        backend: ConfirmationBackend,
        *,
        ttl_seconds: int = DEFAULT_CONFIRM_TTL_SECONDS,
        clock: Clock = time.time,
        max_outstanding_per_identity: int = MAX_OUTSTANDING_PER_IDENTITY,
    ) -> None:
        self._backend = backend
        self._ttl = ttl_seconds
        self._clock = clock
        self._max_outstanding = max_outstanding_per_identity
        self._outstanding: dict[str, dict[str, float]] = {}

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    async def issue(self, *, identity: str, action: str, payload: Any) -> str:
        token = secrets.token_urlsafe(32)
        key = _storage_key(token)
        expires_at = self._clock() + self._ttl
        value = json.dumps(asdict(_Record(identity, action, payload_digest(payload), expires_at)))
        self._reserve(identity, key, expires_at)
        try:
            await self._backend.put(key, value, self._ttl)
        except BaseException:
            self._release(identity, key)
            raise
        return token

    async def consume(self, token: str, *, identity: str, action: str, payload: Any) -> None:
        """Spend ``token``; raise :class:`ConfirmationError` unless it matches this exact request."""
        key = _storage_key(token)
        raw = await self._backend.take(key) if token else None
        if raw is None:
            raise ConfirmationError("confirmation token is unknown, already used or expired; prepare the action again")
        record = _Record(**json.loads(raw))
        self._release(record.identity, key)
        if record.expires_at <= self._clock():
            raise ConfirmationError("confirmation token expired; prepare the action again")
        expected = (identity, action, payload_digest(payload))
        actual = (record.identity, record.action, record.payload_sha256)
        if not all(hmac.compare_digest(a.encode(), b.encode()) for a, b in zip(expected, actual, strict=True)):
            raise ConfirmationError("confirmation token does not match this request; prepare the action again")

    def _live(self, identity: str) -> dict[str, float]:
        now = self._clock()
        return {key: expiry for key, expiry in self._outstanding.get(identity, {}).items() if expiry > now}

    def _reserve(self, identity: str, key: str, expires_at: float) -> None:
        live = self._live(identity)
        if len(live) >= self._max_outstanding:
            raise ConfirmationLimitError(
                f"you already have {len(live)} actions waiting for confirmation; confirm them or let them expire "
                f"(each lasts {self._ttl} seconds) before preparing another"
            )
        now = self._clock()
        others = {
            other: holds
            for other, holds in self._outstanding.items()
            if other != identity and any(expiry > now for expiry in holds.values())
        }
        self._outstanding = {**others, identity: {**live, key: expires_at}}

    def _release(self, identity: str, key: str) -> None:
        remaining = {other: expiry for other, expiry in self._live(identity).items() if other != key}
        others = {other: keys for other, keys in self._outstanding.items() if other != identity}
        self._outstanding = {**others, identity: remaining} if remaining else others
