"""Turns Chatwoot's large replies into short ones, and marks customer-written text as untrusted.

Chatwoot sends a lot of data the assistant does not need (avatars, browser details, nested copies of the
conversation). Keeping only the useful fields saves the assistant's working space and keeps its answers on
topic. Anything a customer wrote is wrapped in <untrusted_content> so the assistant treats it as data to
read, never as instructions to follow.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_OPEN = "<untrusted_content>"
_CLOSE = "</untrusted_content>"
# Any spelling an assistant might still read as the closing tag: other case, spaces inside.
_CLOSE_LOOKALIKE = re.compile(r"<\s*/\s*untrusted_content\s*>", re.IGNORECASE)
_MESSAGE_TYPES = {0: "incoming", 1: "outgoing", 2: "activity", 3: "template"}
_ATTRIBUTE_VALUE_LIMIT = 200


def wrap_untrusted(text: str | None, limit: int) -> str | None:
    if not text:
        return text
    shown = text[:limit]
    dropped = len(text) - len(shown)
    note = f"\n[truncated {dropped} more characters]" if dropped else ""
    # A customer could type the closing tag to break out of the wrapper, so neutralise any they typed.
    safe = _CLOSE_LOOKALIKE.sub("</untrusted_content (removed)>", shown)
    return f"{_OPEN}{safe}{note}{_CLOSE}"


def limit_items(items: Sequence[Any] | None, maximum: int) -> tuple[list[Any], bool]:
    items = list(items or [])
    return items[:maximum], len(items) > maximum


def person(raw: Any, *fields: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    return {field: raw.get(field) for field in fields}


def rows(data: Any) -> list[Any]:
    """Chatwoot answers some lists as a bare array and some as {"payload": [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("payload"), list):
        return data["payload"]
    return []


def compact_message(raw: dict[str, Any], text_limit: int) -> dict[str, Any]:
    kind = raw.get("message_type")
    out: dict[str, Any] = {
        "id": raw.get("id"),
        "type": _MESSAGE_TYPES.get(kind, kind if kind in _MESSAGE_TYPES.values() else "unknown"),
        "private": bool(raw.get("private", False)),
    }
    if raw.get("created_at") is not None:
        out["created_at"] = raw["created_at"]
    if raw.get("content") is not None:
        out["content"] = wrap_untrusted(raw["content"], text_limit)
    sender = person(raw.get("sender"), "id", "name", "type")
    if sender:
        out["sender"] = sender
    attachments = [{"id": a.get("id"), "file_type": a.get("file_type")} for a in raw.get("attachments") or []]
    if attachments:
        out["attachments"] = attachments
    return out


def compact_conversation(raw: dict[str, Any], text_limit: int) -> dict[str, Any]:
    meta = raw.get("meta") or {}
    last = raw.get("last_non_activity_message")
    return {
        "id": raw.get("id"),
        "status": raw.get("status"),
        "priority": raw.get("priority"),
        "inbox_id": raw.get("inbox_id"),
        "channel": meta.get("channel"),
        "labels": raw.get("labels") or [],
        "unread_count": raw.get("unread_count"),
        "can_reply": raw.get("can_reply"),
        "muted": raw.get("muted"),
        "snoozed_until": raw.get("snoozed_until"),
        "created_at": raw.get("created_at"),
        "last_activity_at": raw.get("last_activity_at"),
        "waiting_since": raw.get("waiting_since"),
        "contact": person(meta.get("sender"), "id", "name", "email", "phone_number"),
        "assignee": person(meta.get("assignee"), "id", "name", "email"),
        "team": person(meta.get("team"), "id", "name"),
        "last_message": compact_message(last, text_limit) if isinstance(last, dict) else None,
    }


def _attribute_value(value: Any) -> Any:
    # The website widget lets visitors set their own custom attributes, so text values are untrusted.
    return wrap_untrusted(value, _ATTRIBUTE_VALUE_LIMIT) if isinstance(value, str) else value


def compact_contact(raw: dict[str, Any]) -> dict[str, Any]:
    custom = raw.get("custom_attributes") or {}
    return {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "email": raw.get("email"),
        "phone_number": raw.get("phone_number"),
        "identifier": raw.get("identifier"),
        "blocked": raw.get("blocked"),
        "created_at": raw.get("created_at"),
        "last_activity_at": raw.get("last_activity_at"),
        "custom_attributes": {k: _attribute_value(v) for k, v in custom.items()},
    }
