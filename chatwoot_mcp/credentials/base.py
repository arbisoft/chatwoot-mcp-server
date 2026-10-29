"""The credential every tool call runs with, and how a mode supplies it.

A :class:`CredentialProvider` answers "which Chatwoot key, and whose, does this request use?". Community mode has
two (``static``, ``header``); platform mode adds one that fetches each signed-in user's own key. Tools only ever
see the protocol.

The identity of a key-based credential is ``sha256(key)[:16]``: stable enough to key a memo and a confirmation on,
and never the raw key.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from chatwoot_mcp.client import ChatwootClient, ChatwootError
from chatwoot_mcp.client.models import Profile as ChatwootProfile
from chatwoot_mcp.tiers import Tier, tier_for

IDENTITY_LENGTH = 16
PROFILE_PATH = "/api/v1/profile"

Clock = Callable[[], float]


def token_identity(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:IDENTITY_LENGTH]


@dataclass(frozen=True)
class Profile:
    """What the key's owner may do in the configured account, read from their Chatwoot profile."""

    tier: Tier | None = None
    permissions: frozenset[str] = frozenset()

    @property
    def known(self) -> bool:
        return self.tier is not None


UNKNOWN_PROFILE = Profile()


@dataclass(frozen=True)
class ChatwootCredential:
    token: str = field(repr=False)
    identity: str
    tier: Tier | None
    permissions: frozenset[str]

    @classmethod
    def for_token(cls, token: str, profile: Profile) -> ChatwootCredential:
        return cls(token=token, identity=token_identity(token), tier=profile.tier, permissions=profile.permissions)


class CredentialProvider(Protocol):
    async def resolve(self) -> ChatwootCredential:
        """Return the credential for the current request, or raise a :class:`ChatwootError`."""
        ...


def profile_from(profile: ChatwootProfile, account_id: int) -> Profile:
    account = profile.account(account_id)
    permissions = frozenset(account.permissions) if account else frozenset()
    return Profile(tier=tier_for(account.permissions if account else None), permissions=permissions)


async def fetch_profile(client: ChatwootClient, token: str, account_id: int) -> Profile:
    """The key owner's role in the configured account, from ``GET /api/v1/profile``."""
    return profile_from(ChatwootProfile.parse(await client.get(PROFILE_PATH, token=token)), account_id)


ProfileLoader = Callable[[], Awaitable[Profile]]


class ProfileMemo:
    """In-process memo of profiles, keyed by credential identity.

    Holds at most ``max_entries`` identities and evicts the least recently stored first. A failed lookup is
    remembered as unknown for ``failure_ttl`` seconds so a broken lookup costs one extra request a minute, not one
    per call. The memo is the one deliberately mutable object here: it is a cache.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float | None,
        failure_ttl_seconds: float = 60.0,
        max_entries: int = 1024,
        clock: Clock = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._failure_ttl = failure_ttl_seconds
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: OrderedDict[str, tuple[Profile, float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def size(self) -> int:
        return len(self._entries)

    def _fresh(self, identity: str) -> Profile | None:
        entry = self._entries.get(identity)
        if entry is None or entry[1] <= self._clock():
            return None
        return entry[0]

    def _store(self, identity: str, profile: Profile) -> None:
        ttl = self._ttl if profile.known else self._failure_ttl
        self._entries[identity] = (profile, float("inf") if ttl is None else self._clock() + ttl)
        self._entries.move_to_end(identity)
        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            self._release_idle_lock(evicted)

    def _release_idle_lock(self, identity: str) -> None:
        lock = self._locks.get(identity)
        if lock is not None and not lock.locked():
            del self._locks[identity]

    async def get(self, identity: str, load: ProfileLoader) -> Profile:
        cached = self._fresh(identity)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(identity, asyncio.Lock())
        try:
            async with lock:
                cached = self._fresh(identity)
                if cached is not None:
                    return cached
                profile = await _load_or_unknown(load)
                self._store(identity, profile)
                return profile
        finally:
            if identity not in self._entries:
                self._release_idle_lock(identity)


async def _load_or_unknown(load: ProfileLoader) -> Profile:
    try:
        return await load()
    except ChatwootError:
        return UNKNOWN_PROFILE
