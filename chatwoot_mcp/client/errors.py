"""Errors raised by the Chatwoot client.

Tools never let these escape: :func:`to_tool_error` turns any of them into the short ``Error: ...`` string a tool
returns to the model. No message ever carries the caller's key: the client strips it from Chatwoot's own error
text before building one.
"""

from __future__ import annotations

import httpx

# Chatwoot answers 401 both for a bad key and for a role that may not do something (Pundit's refusal);
# only this text tells them apart.
NOT_AUTHORIZED_DETAIL = "You are not authorized to do this action"
DETAIL_LIMIT = 300


class ChatwootError(Exception):
    """Base class for every failure talking to Chatwoot."""


class IdentityHeaderError(ChatwootError):
    """An ``X-Auth-Request-*`` header was about to leave the process.

    Those headers are trusted by an SSO-fronted Chatwoot as proof of identity. The client must never send one,
    whatever the caller supplied.
    """


class MissingTokenError(ChatwootError):
    """No Chatwoot key is available for this call."""


class InvalidTokenError(ChatwootError):
    """The key cannot be sent as a header (non-ASCII, control characters or spaces)."""


class UnexpectedResponseError(ChatwootError):
    """Chatwoot answered successfully, but not with the shape this server expects."""


class ChatwootTransportError(ChatwootError):
    """The request never produced a response (DNS, connect, timeout)."""


class ChatwootAPIError(ChatwootError):
    """Chatwoot answered with a non-success HTTP status."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class RedirectedError(ChatwootAPIError):
    """HTTP 3xx: redirects are never followed. Behind Moneta SSO this is usually the login page in front of /api."""


class AuthError(ChatwootAPIError):
    """HTTP 401: the key is wrong, reset or disabled."""


class PermissionDenied(ChatwootAPIError):
    """The caller's Chatwoot role does not allow this (HTTP 403, or Chatwoot's role-refusal 401)."""


class GatewayDeniedError(PermissionDenied):
    """HTTP 403 from an SSO edge in front of Chatwoot (``{"error": "access_denied"}``), not from Chatwoot's roles."""


class NotFoundError(ChatwootAPIError):
    """HTTP 404: the object does not exist, is in another account, or is not visible to this user."""


class UnprocessableError(ChatwootAPIError):
    """HTTP 422: Chatwoot rejected the request body."""


class RateLimitedError(ChatwootAPIError):
    """HTTP 429: Chatwoot is throttling this client."""


class ServerError(ChatwootAPIError):
    """HTTP 5xx: Chatwoot, or the proxy in front of it, failed."""


_ERRORS_BY_STATUS: dict[int, type[ChatwootAPIError]] = {
    404: NotFoundError,
    422: UnprocessableError,
    429: RateLimitedError,
}


def error_for_status(status_code: int, detail: str) -> ChatwootAPIError:
    if 300 <= status_code < 400:
        return RedirectedError(status_code, detail)
    if status_code == 401:
        return (PermissionDenied if detail == NOT_AUTHORIZED_DETAIL else AuthError)(status_code, detail)
    if status_code == 403:
        return (GatewayDeniedError if "access_denied" in detail.lower() else PermissionDenied)(status_code, detail)
    if status_code >= 500:
        return ServerError(status_code, detail)
    return _ERRORS_BY_STATUS.get(status_code, ChatwootAPIError)(status_code, detail)


def error_detail(response: httpx.Response) -> str:
    fallback = response.reason_phrase or "unexpected response"
    try:
        body = response.json()
    except ValueError:
        return fallback
    if not isinstance(body, dict):
        return fallback
    return (_error_message(body) or fallback)[:DETAIL_LIMIT]


def _error_message(body: dict) -> str | None:
    """Chatwoot uses ``error``, ``message`` or ``description``, and a list under ``errors`` for validation."""
    for key in ("message", "error", "description"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        return "; ".join(str(item) for item in errors)
    return None


def to_tool_error(error: ChatwootError) -> str:
    match error:
        case RedirectedError():
            return (
                f"Error: Chatwoot redirected the request (HTTP {error.status_code}) instead of answering, often to a "
                "login page; the server must call Chatwoot's internal address"
            )
        case GatewayDeniedError():
            return (
                "Error: access denied by the sign-in gateway in front of Chatwoot (HTTP 403); "
                "this account is not allowed to use this Chatwoot"
            )
        case PermissionDenied():
            return f"Error: permission denied (HTTP {error.status_code}); your Chatwoot role does not allow this"
        case AuthError():
            return f"Error: Chatwoot rejected the key (HTTP {error.status_code}); it is wrong, reset or disabled"
        case NotFoundError():
            return "Error: not found (HTTP 404); check the id, and that it belongs to the configured account"
        case UnprocessableError():
            return f"Error: Chatwoot refused the request (HTTP 422): {error.detail}"
        case RateLimitedError():
            return "Error: Chatwoot is rate limiting requests (HTTP 429); try again shortly"
        case ServerError():
            return f"Error: Chatwoot had a server error (HTTP {error.status_code}); try again later"
        case ChatwootAPIError():
            return f"Error: Chatwoot returned HTTP {error.status_code} ({error.detail})"
    return f"Error: {error}"
