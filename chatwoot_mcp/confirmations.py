"""The "are you sure?" step for actions that change things.

First call to an action tool changes nothing: it returns a preview and a one-time token. The assistant must
show the preview to the person and only then call the tool again, with the same arguments and the token.

A token is tied to who asked (a hash of their key), which tool, and the exact arguments, so it cannot be used
by someone else, for a different action, or after the request was edited. It works once and expires.

Tokens are kept in this process's memory. That is enough for one server process; if the server is ever run as
several processes behind a load balancer, the tokens need shared storage.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


def fingerprint(tool: str, arguments: Mapping[str, Any]) -> str:
    canonical = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class _Pending:
    owner: str
    digest: str
    expires_at: float


class Confirmations:
    def __init__(
        self, *, ttl_seconds: int = 300, max_entries: int = 1000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._pending: OrderedDict[str, _Pending] = OrderedDict()

    def issue(self, owner: str, tool: str, arguments: Mapping[str, Any]) -> str:
        token = secrets.token_urlsafe(24)
        self._pending[token] = _Pending(owner, fingerprint(tool, arguments), self._clock() + self.ttl_seconds)
        while len(self._pending) > self._max_entries:
            self._pending.popitem(last=False)
        return token

    def redeem(self, owner: str, tool: str, arguments: Mapping[str, Any], token: str) -> bool:
        """True only if the token is known, unexpired, and matches who, what and the exact arguments. Uses it up."""
        pending = self._pending.pop(token, None) if token else None
        return (
            pending is not None
            and pending.expires_at > self._clock()
            and secrets.compare_digest(pending.owner, owner)
            and secrets.compare_digest(pending.digest, fingerprint(tool, arguments))
        )
