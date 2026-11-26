"""Request and response schemas for the public HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class CreateUserRequest(StrictModel):
    display_name: str = Field(min_length=1, max_length=80)

    @field_validator("display_name")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("display_name must not be blank")
        return cleaned


class CreateConversationRequest(StrictModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _clean(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned


class AppendMessageRequest(StrictModel):
    body: str = Field(min_length=1, max_length=20_000)
    client_token: str | None = Field(default=None, max_length=200)
    # Members the author explicitly points out; each must be a current member
    # of the conversation. Duplicates are collapsed, blanks rejected.
    mention_user_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("body")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be blank")
        return value

    @field_validator("client_token")
    @classmethod
    def _token_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("client_token must not be blank")
        return value

    @field_validator("mention_user_ids")
    @classmethod
    def _clean_mentions(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in value:
            user_id = raw.strip()
            if not user_id:
                raise ValueError("mention_user_ids must not contain blank ids")
            if len(user_id) > 100:
                raise ValueError("mention_user_ids entries are too long")
            if user_id not in cleaned:
                cleaned.append(user_id)
        return cleaned


class ForkRequest(StrictModel):
    parent_message_id: str = Field(min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned


class AddMemberRequest(StrictModel):
    user_id: str = Field(min_length=1, max_length=100)


class ClaimRequest(StrictModel):
    # Idempotency key, unique per user: retrying the same request_id returns
    # the original batch (minus lapsed items) instead of claiming anew.
    request_id: str = Field(min_length=1, max_length=200)
    limit: int | None = Field(default=None, ge=1, le=100)

    @field_validator("request_id")
    @classmethod
    def _request_id_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request_id must not be blank")
        return value


class ConfirmRequest(StrictModel):
    batch_id: str = Field(min_length=1, max_length=100)
    notification_id: str = Field(min_length=1, max_length=100)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


class UserRef(BaseModel):
    id: str
    display_name: str


class UserOut(UserRef):
    created_at: str


class MessageOrigin(BaseModel):
    """Where the one original message was authored.

    The triple (message_id, origin.conversation_id, origin.position) stays
    constant in every branch that inherits the message. It is a reference, not
    a copied utterance.
    """

    conversation_id: str
    position: int


class MessageOut(BaseModel):
    message_id: str
    # Position inside the conversation being read (inherited prefix + local).
    position: int
    # "inherited" when this message predates the fork; "local" otherwise.
    source: Literal["inherited", "local"]
    author: UserRef
    origin: MessageOrigin
    body: str | None
    retracted: bool
    retracted_at: str | None
    created_at: str


class MessagePage(BaseModel):
    conversation_id: str
    items: list[MessageOut]
    has_more: bool
    next_cursor: str | None


class SearchPage(BaseModel):
    conversation_id: str
    query: str
    items: list[MessageOut]
    has_more: bool
    next_cursor: str | None


class ForkPointOut(BaseModel):
    message_id: str
    # Position of the fork point in the *parent* conversation.
    parent_position: int
    origin: MessageOrigin
    retracted: bool


class ConversationOut(BaseModel):
    id: str
    title: str
    created_by: str
    created_at: str
    parent_conversation_id: str | None
    inherited_message_count: int
    fork_point: ForkPointOut | None


class MemberOut(BaseModel):
    membership_id: str
    conversation_id: str
    user: UserRef
    role: Literal["owner", "member"]
    added_by: str | None
    joined_at: str


class ConversationDetail(ConversationOut):
    members: list[MemberOut]


class ForkedAtRetractedPointOut(BaseModel):
    """A conversation whose fork point is the retracted message.

    Such a branch is not severed: it keeps the placeholder (and its own local
    history after the fork point), but it inherits no live content from that
    message.
    """

    conversation_id: str
    fork_point_position: int


class RetractionOut(BaseModel):
    message_id: str
    author: UserRef
    origin: MessageOrigin
    retracted: bool
    retracted_at: str
    affected_conversations: int
    already_retracted: bool
    forked_at_retracted_point: list[ForkedAtRetractedPointOut]


class EventOut(BaseModel):
    seq: int
    kind: str
    conversation_id: str
    actor_id: str
    message_id: str | None
    details: dict[str, Any]
    occurred_at: str


class FollowOut(BaseModel):
    conversation_id: str
    user_id: str
    following: bool
    followed_at: str | None


class NotificationOut(BaseModel):
    notification_id: str
    # The conversation the original message was authored in.
    conversation_id: str
    message: MessageOut
    # Trigger reasons that are still live at response time.
    reasons: list[Literal["follow", "mention"]]
    created_at: str


class ClaimBatchOut(BaseModel):
    # batch_id is the credential: it confirms this batch's items, only until
    # expires_at, and only while each item's reasons are still live.
    batch_id: str
    request_id: str
    expires_at: str
    items: list[NotificationOut]


class ConfirmOut(BaseModel):
    notification_id: str
    batch_id: str
    confirmed: bool
    already_confirmed: bool
    confirmed_at: str
    # Present on the first successful confirmation only; a retried confirm
    # changes nothing and carries no body.
    message: NotificationOut | None


class HealthOut(BaseModel):
    status: Literal["ok"]
    database: str
