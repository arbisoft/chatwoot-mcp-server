"""Tolerant views of Chatwoot objects.

Every model allows extra fields and coerces numbers to strings where a string is declared, so a Chatwoot upgrade
that adds or retypes an attribute never breaks a tool. Only the fields the shapers read are declared. Lists come
back from Chatwoot either bare or as ``{"payload": [...]}``; :func:`rows` reads both.
"""

from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from chatwoot_mcp.client.errors import UnexpectedResponseError

Timestamp = int | float | str | None


class ChatwootModel(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True, populate_by_name=True)

    @classmethod
    def parse(cls, data: Any) -> Self:
        """Validate a Chatwoot response, raising :class:`UnexpectedResponseError` instead of pydantic's error."""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise UnexpectedResponseError(f"Chatwoot returned an unexpected {cls.__name__} shape") from exc

    @classmethod
    def parse_rows(cls, data: Any) -> list[Self]:
        return [cls.parse(row) for row in rows(data) if isinstance(row, dict)]


def rows(data: Any) -> list[Any]:
    """Chatwoot answers some lists as a bare array and some as ``{"payload": [...]}``."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("payload"), list):
        return data["payload"]
    return []


def payload(data: Any) -> dict[str, Any]:
    """The object inside ``{"payload": {...}}``, or the answer itself."""
    if isinstance(data, dict):
        inner = data.get("payload", data)
        return inner if isinstance(inner, dict) else {}
    return {}


class Person(ChatwootModel):
    """A sender, assignee or contact as Chatwoot nests them: every field optional."""

    id: int | None = None
    name: str | None = None
    email: str | None = None
    phone_number: str | None = None
    type: str | None = None


class Team(ChatwootModel):
    id: int
    name: str | None = None
    description: str | None = None


class Agent(ChatwootModel):
    id: int
    name: str | None = None
    email: str | None = None
    role: str | None = None
    availability_status: str | None = None


class Attachment(ChatwootModel):
    id: int
    message_id: int | None = None
    file_type: str | None = None
    extension: str | None = None
    file_size: int | None = None
    created_at: Timestamp = None
    sender: Person | None = None


class Message(ChatwootModel):
    id: int
    content: str | None = None
    message_type: int | str | None = None
    private: bool = False
    created_at: Timestamp = None
    sender: Person | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class ConversationMeta(ChatwootModel):
    channel: str | None = None
    sender: Person | None = None
    assignee: Person | None = None
    team: Team | None = None


class Conversation(ChatwootModel):
    id: int
    status: str | None = None
    priority: str | None = None
    inbox_id: int | None = None
    labels: list[str] = Field(default_factory=list)
    unread_count: int | None = None
    can_reply: bool | None = None
    muted: bool | None = None
    snoozed_until: Timestamp = None
    created_at: Timestamp = None
    last_activity_at: Timestamp = None
    waiting_since: Timestamp = None
    custom_attributes: dict[str, Any] = Field(default_factory=dict)
    meta: ConversationMeta = Field(default_factory=ConversationMeta)
    last_non_activity_message: Message | None = None


class Contact(ChatwootModel):
    id: int
    name: str | None = None
    email: str | None = None
    phone_number: str | None = None
    identifier: str | None = None
    blocked: bool | None = None
    created_at: Timestamp = None
    last_activity_at: Timestamp = None
    custom_attributes: dict[str, Any] = Field(default_factory=dict)


class Note(ChatwootModel):
    id: int
    content: str | None = None
    user: Person | None = None
    created_at: Timestamp = None


class Inbox(ChatwootModel):
    id: int
    name: str | None = None
    channel_type: str | None = None
    website_url: str | None = None
    phone_number: str | None = None
    business_name: str | None = None
    greeting_enabled: bool | None = None
    greeting_message: str | None = None
    working_hours_enabled: bool | None = None
    timezone: str | None = None
    csat_survey_enabled: bool | None = None
    enable_auto_assignment: bool | None = None
    allow_messages_after_resolved: bool | None = None
    lock_to_single_conversation: bool | None = None


class Label(ChatwootModel):
    id: int
    title: str
    description: str | None = None


class CannedResponse(ChatwootModel):
    id: int
    short_code: str | None = None
    content: str | None = None


class CustomAttributeDefinition(ChatwootModel):
    id: int
    attribute_key: str
    attribute_display_name: str | None = None
    attribute_display_type: str | None = None
    attribute_description: str | None = None
    attribute_values: list[Any] = Field(default_factory=list)
    attribute_model: str | None = None


class Account(ChatwootModel):
    id: int
    name: str | None = None
    locale: str | None = None
    status: str | None = None
    domain: str | None = None
    support_email: str | None = None
    created_at: Timestamp = None


class ProfileAccount(ChatwootModel):
    id: int
    name: str | None = None
    role: str | None = None
    permissions: list[str] = Field(default_factory=list)


class Profile(ChatwootModel):
    """``GET /api/v1/profile``. It also holds the user's access and pubsub tokens, which are never declared here."""

    id: int
    name: str | None = None
    email: str | None = None
    availability_status: str | None = None
    accounts: list[ProfileAccount] = Field(default_factory=list)

    def account(self, account_id: int) -> ProfileAccount | None:
        return next((account for account in self.accounts if account.id == account_id), None)


class Article(ChatwootModel):
    id: int
    title: str | None = None
    status: str | None = None
    category_name: str | None = None
    locale: str | None = None
    portal_slug: str | None = None
    slug: str | None = None
    content: str | None = None
    updated_at: Timestamp = None


class CsatResponse(ChatwootModel):
    id: int
    rating: int | None = None
    feedback_message: str | None = None
    conversation_id: int | None = None
    contact: Person | None = None
    assigned_agent: Person | None = None
    created_at: Timestamp = None


class MacroAction(ChatwootModel):
    action_name: str
    action_params: Any = None


class Macro(ChatwootModel):
    id: int
    name: str | None = None
    visibility: str | None = None
    actions: list[MacroAction] = Field(default_factory=list)
