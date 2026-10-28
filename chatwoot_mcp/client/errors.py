"""Errors shown to the person using the AI assistant. The text must be safe to show: it never contains the
Chatwoot key, and it says in plain words what went wrong."""

from __future__ import annotations

_BY_STATUS = {
    401: "Chatwoot said the key was rejected. The key may be wrong, expired or disabled.",
    403: "Chatwoot said this is not allowed for your key. Your Chatwoot role does not cover it.",
    404: "Chatwoot said that was not found. Check the id, and that it belongs to the configured account.",
    429: "Chatwoot said there are too many requests. Wait a little and try again.",
}


class ChatwootError(Exception):
    """A Chatwoot call failed. `str(error)` is safe to show to the user."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def error_for_status(status: int, body: object) -> ChatwootError:
    if status in _BY_STATUS:
        return ChatwootError(_BY_STATUS[status], status)
    if status == 422:
        reason = _reason(body)
        suffix = f" Reason: {reason}" if reason else ""
        return ChatwootError(f"Chatwoot refused the request.{suffix}", status)
    if status >= 500:
        return ChatwootError(f"Chatwoot failed on its side (HTTP {status}). Try again later.", status)
    return ChatwootError(f"Chatwoot answered with an unexpected status (HTTP {status}).", status)


def _reason(body: object) -> str:
    if isinstance(body, dict):
        for key in ("message", "error", "description"):
            value = body.get(key)
            if isinstance(value, str):
                return value[:300]
    return ""
