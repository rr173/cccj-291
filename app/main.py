"""HTTP surface.

Identity is carried by the ``X-User-Id`` header (no auth provider needed for a
local, reproducible service). Every conversation route requires membership;
nothing is inherited from a parent conversation's membership.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response

from .errors import (
    ApiError,
    api_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from .models import (
    AddMemberRequest,
    AppendMessageRequest,
    ApproveResolutionProposalRequest,
    ClaimBatchOut,
    ClaimRequest,
    ConfirmOut,
    ConfirmRequest,
    ConversationDetail,
    ConversationOut,
    CreateConversationRequest,
    CreateResolutionProposalRequest,
    CreateUserRequest,
    EventOut,
    FollowOut,
    ForkRequest,
    HealthOut,
    MemberOut,
    MessageOut,
    MessagePage,
    RetractionOut,
    SearchPage,
    SyncPullRequest,
    SyncPullResponse,
    UserOut,
)
from .service import ConversationService


def default_database_path() -> str:
    return os.environ.get(
        "CONVERSATIONS_DB",
        str(Path(__file__).resolve().parent.parent / "data" / "conversations.sqlite3"),
    )


def get_service(request: Request) -> ConversationService:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise ApiError(503, "not_ready", "service is starting up")
    return service


def current_user_id(
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> str:
    if x_user_id is None or not x_user_id.strip():
        raise ApiError(401, "missing_user", "the X-User-Id header is required")
    return x_user_id.strip()


ServiceDep = Annotated[ConversationService, Depends(get_service)]
UserDep = Annotated[str, Depends(current_user_id)]


def create_app(database_path: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = ConversationService(database_path or default_database_path())
        yield

    app = FastAPI(
        title="Branched Conversations",
        version="1.0.0",
        description=(
            "Multi-user conversations with fork-from-here and global, "
            "author-driven retraction."
        ),
        lifespan=lifespan,
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    @app.get("/health", response_model=HealthOut, tags=["system"])
    def health(service: ServiceDep) -> dict[str, Any]:
        return {"status": "ok", "database": service.db.path}

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    @app.post(
        "/users",
        response_model=UserOut,
        status_code=201,
        tags=["users"],
        summary="Register a user",
    )
    def create_user(body: CreateUserRequest, service: ServiceDep) -> dict[str, Any]:
        return service.create_user(body.display_name)

    @app.get(
        "/users/{user_id}",
        response_model=UserOut,
        tags=["users"],
        summary="Look up a user",
    )
    def get_user(user_id: str, service: ServiceDep) -> dict[str, Any]:
        return service.get_user(user_id)

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    @app.post(
        "/conversations",
        response_model=ConversationDetail,
        status_code=201,
        tags=["conversations"],
        summary="Create a conversation (creator becomes sole owner)",
    )
    def create_conversation(
        body: CreateConversationRequest, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.create_conversation(user_id, body.title)

    @app.get(
        "/conversations",
        response_model=list[ConversationOut],
        tags=["conversations"],
        summary="List conversations the caller currently belongs to",
    )
    def list_conversations(
        user_id: UserDep, service: ServiceDep
    ) -> list[dict[str, Any]]:
        service.authenticate(user_id)
        return service.list_conversations(user_id)

    @app.get(
        "/conversations/{conversation_id}",
        response_model=ConversationDetail,
        tags=["conversations"],
        summary="Conversation metadata, fork provenance and members",
    )
    def get_conversation(
        conversation_id: str, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.get_conversation(conversation_id, user_id)

    @app.post(
        "/conversations/{conversation_id}/fork",
        response_model=ConversationDetail,
        status_code=201,
        tags=["conversations"],
        summary="Fork from one currently visible message (declared before append)",
    )
    def fork_conversation(
        conversation_id: str,
        body: ForkRequest,
        user_id: UserDep,
        service: ServiceDep,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.fork_conversation(
            conversation_id, user_id, body.parent_message_id, body.title
        )

    # ------------------------------------------------------------------
    # Members
    # ------------------------------------------------------------------

    @app.get(
        "/conversations/{conversation_id}/members",
        response_model=list[MemberOut],
        tags=["members"],
        summary="List members of this conversation only",
    )
    def list_members(
        conversation_id: str, user_id: UserDep, service: ServiceDep
    ) -> list[dict[str, Any]]:
        service.authenticate(user_id)
        return service.list_members(conversation_id, user_id)

    @app.post(
        "/conversations/{conversation_id}/members",
        response_model=MemberOut,
        status_code=201,
        tags=["members"],
        summary="Invite a user to this conversation (owner only)",
    )
    def add_member(
        conversation_id: str,
        body: AddMemberRequest,
        user_id: UserDep,
        service: ServiceDep,
        response: Response,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        member, created = service.add_member(conversation_id, user_id, body.user_id)
        if not created:
            response.status_code = 200
        return member

    @app.delete(
        "/conversations/{conversation_id}/members/{member_user_id}",
        status_code=204,
        tags=["members"],
        summary="Remove a member; their outstanding page cursors stop working",
    )
    def remove_member(
        conversation_id: str,
        member_user_id: str,
        user_id: UserDep,
        service: ServiceDep,
    ) -> Response:
        service.authenticate(user_id)
        service.remove_member(conversation_id, user_id, member_user_id)
        return Response(status_code=204)

    # ------------------------------------------------------------------
    # Follows (notification subscription for one conversation)
    # ------------------------------------------------------------------

    @app.post(
        "/conversations/{conversation_id}/follow",
        response_model=FollowOut,
        status_code=201,
        tags=["follows"],
        summary="Follow this conversation; 200 when already following",
    )
    def follow_conversation(
        conversation_id: str,
        user_id: UserDep,
        service: ServiceDep,
        response: Response,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        status, created = service.follow_conversation(conversation_id, user_id)
        if not created:
            response.status_code = 200
        return status

    @app.delete(
        "/conversations/{conversation_id}/follow",
        status_code=204,
        tags=["follows"],
        summary="Unfollow; idempotent, kills only this follow instance's reasons",
    )
    def unfollow_conversation(
        conversation_id: str, user_id: UserDep, service: ServiceDep
    ) -> Response:
        service.authenticate(user_id)
        service.unfollow_conversation(conversation_id, user_id)
        return Response(status_code=204)

    @app.get(
        "/conversations/{conversation_id}/follow",
        response_model=FollowOut,
        tags=["follows"],
        summary="Whether the caller currently follows this conversation",
    )
    def get_follow_status(
        conversation_id: str, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.get_follow_status(conversation_id, user_id)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    @app.post(
        "/conversations/{conversation_id}/messages",
        response_model=MessageOut,
        status_code=201,
        tags=["messages"],
        summary="Append an immutable message (idempotent with client_token)",
    )
    def append_message(
        conversation_id: str,
        body: AppendMessageRequest,
        user_id: UserDep,
        service: ServiceDep,
        response: Response,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        message, created = service.append_message(
            conversation_id,
            user_id,
            body.body,
            body.client_token,
            body.mention_user_ids,
        )
        # A retry returns the original result rather than creating a new one.
        if not created:
            response.status_code = 200
        return message

    @app.get(
        "/conversations/{conversation_id}/messages",
        response_model=MessagePage,
        tags=["messages"],
        summary="Page through inherited + local history in order",
    )
    def list_messages(
        conversation_id: str,
        user_id: UserDep,
        service: ServiceDep,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
        cursor: Annotated[str | None, Query(max_length=4096)] = None,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.list_messages(conversation_id, user_id, limit, cursor)

    @app.get(
        "/conversations/{conversation_id}/messages/{message_id}",
        response_model=MessageOut,
        tags=["messages"],
        summary="Read one message through this branch's view (with its origin)",
    )
    def get_message(
        conversation_id: str,
        message_id: str,
        user_id: UserDep,
        service: ServiceDep,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.get_message(conversation_id, user_id, message_id)

    @app.get(
        "/conversations/{conversation_id}/search",
        response_model=SearchPage,
        tags=["messages"],
        summary="Case-insensitive keyword search inside this branch's view",
    )
    def search_messages(
        conversation_id: str,
        user_id: UserDep,
        service: ServiceDep,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
        cursor: Annotated[str | None, Query(max_length=4096)] = None,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.search_messages(conversation_id, user_id, q, limit, cursor)

    @app.post(
        "/messages/{message_id}/retraction",
        response_model=RetractionOut,
        tags=["messages"],
        summary="Author retracts the original message everywhere (idempotent)",
    )
    def retract_message(
        message_id: str, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.retract_message(message_id, user_id)

    # ------------------------------------------------------------------
    # Resolution proposals, parent-board review and approval
    # ------------------------------------------------------------------

    @app.post(
        "/conversations/{conversation_id}/resolution-proposals",
        status_code=201,
        tags=["resolutions"],
        summary=(
            "Submit an atomic resolution package from a child branch. With a "
            "child_conversation_id body field, this path is the direct parent."
        ),
    )
    @app.post(
        "/conversations/{conversation_id}/proposals",
        status_code=201,
        tags=["resolutions"],
        include_in_schema=False,
    )
    def create_resolution_proposal(
        conversation_id: str,
        body: CreateResolutionProposalRequest,
        user_id: UserDep,
        service: ServiceDep,
        response: Response,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        if (
            body.child_conversation_id is not None
            and body.child_conversation_id != conversation_id
        ):
            child_id = body.child_conversation_id
            expected_parent_id: str | None = conversation_id
        else:
            child_id = conversation_id
            expected_parent_id = None
        proposal, created = service.create_resolution_proposal(
            child_id,
            user_id,
            body.service_items(),
            client_token=body.client_token,
            expected_parent_conversation_id=expected_parent_id,
        )
        response.status_code = 201 if created else 200
        return proposal

    @app.post(
        "/resolution-proposals",
        status_code=201,
        tags=["resolutions"],
        include_in_schema=False,
        summary="Create a proposal by explicitly supplying its child conversation",
    )
    def create_resolution_proposal_top_level(
        body: CreateResolutionProposalRequest,
        user_id: UserDep,
        service: ServiceDep,
        response: Response,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        if body.child_conversation_id is None:
            raise ApiError(
                422,
                "validation_error",
                "child_conversation_id is required on POST /resolution-proposals",
            )
        proposal, created = service.create_resolution_proposal(
            body.child_conversation_id,
            user_id,
            body.service_items(),
            client_token=body.client_token,
            expected_parent_conversation_id=body.parent_conversation_id,
        )
        response.status_code = 201 if created else 200
        return proposal

    @app.get(
        "/conversations/{conversation_id}/resolution-proposals",
        tags=["resolutions"],
        summary="List incoming proposals for a parent board (parent owner only)",
    )
    @app.get(
        "/conversations/{conversation_id}/proposals",
        tags=["resolutions"],
        include_in_schema=False,
    )
    def list_resolution_proposals(
        conversation_id: str,
        user_id: UserDep,
        service: ServiceDep,
        status: Annotated[
            str | None, Query(pattern="^(pending|approved|rejected)$")
        ] = None,
    ) -> list[dict[str, Any]]:
        service.authenticate(user_id)
        return service.list_resolution_proposals(
            conversation_id, user_id, status=status
        )

    @app.get(
        "/conversations/{conversation_id}/resolution-board",
        tags=["resolutions"],
        summary="Read the formal resolution board (members of this parent only)",
    )
    @app.get(
        "/conversations/{conversation_id}/board",
        tags=["resolutions"],
        include_in_schema=False,
    )
    @app.get(
        "/conversations/{conversation_id}/resolutions",
        tags=["resolutions"],
        summary="Alias of the resolution board",
    )
    def get_resolution_board(
        conversation_id: str, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.get_resolution_board(conversation_id, user_id)

    @app.get(
        "/conversations/{conversation_id}/resolution-board/{topic_key}",
        tags=["resolutions"],
        summary="Read one topic on the formal resolution board",
    )
    @app.get(
        "/conversations/{conversation_id}/resolutions/{topic_key}",
        tags=["resolutions"],
        include_in_schema=False,
    )
    def get_resolution_topic(
        conversation_id: str,
        topic_key: str,
        user_id: UserDep,
        service: ServiceDep,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.get_resolution_topic(conversation_id, user_id, topic_key)

    @app.get(
        "/conversations/{conversation_id}/resolution-proposals/{proposal_id}",
        tags=["resolutions"],
        include_in_schema=False,
        summary="Parent-scoped alias for reading one proposal",
    )
    @app.get(
        "/conversations/{conversation_id}/proposals/{proposal_id}",
        tags=["resolutions"],
        include_in_schema=False,
    )
    def get_parent_scoped_resolution_proposal(
        conversation_id: str,
        proposal_id: str,
        user_id: UserDep,
        service: ServiceDep,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.get_resolution_proposal(
            proposal_id,
            user_id,
            expected_parent_conversation_id=conversation_id,
        )

    @app.get(
        "/resolution-proposals/{proposal_id}",
        tags=["resolutions"],
        summary=(
            "Read one proposal as its proposer or as the direct parent's owner; "
            "retracted-basis pending text is redacted"
        ),
    )
    @app.get(
        "/proposals/{proposal_id}",
        tags=["resolutions"],
        include_in_schema=False,
    )
    def get_resolution_proposal(
        proposal_id: str, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.get_resolution_proposal(proposal_id, user_id)

    @app.post(
        "/resolution-proposals/{proposal_id}/approve",
        tags=["resolutions"],
        summary=(
            "Approve and atomically apply a package (direct parent owner only); "
            "retrying an approved proposal returns its original approval result"
        ),
    )
    @app.post(
        "/resolution-proposals/{proposal_id}/approval",
        tags=["resolutions"],
        include_in_schema=False,
    )
    @app.post(
        "/resolution-proposals/{proposal_id}/approvals",
        tags=["resolutions"],
        include_in_schema=False,
    )
    @app.post(
        "/proposals/{proposal_id}/approve",
        tags=["resolutions"],
        include_in_schema=False,
    )
    @app.post(
        "/proposals/{proposal_id}/approval",
        tags=["resolutions"],
        include_in_schema=False,
    )
    def approve_resolution_proposal(
        proposal_id: str,
        user_id: UserDep,
        service: ServiceDep,
        response: Response,
        body: ApproveResolutionProposalRequest | None = None,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        request_id = body.request_id if body is not None else None
        result, created = service.approve_resolution_proposal(
            proposal_id, user_id, request_id=request_id
        )
        response.status_code = 201 if created else 200
        return result

    @app.post(
        "/conversations/{conversation_id}/resolution-proposals/{proposal_id}/approve",
        tags=["resolutions"],
        include_in_schema=False,
        summary="Parent-scoped alias for approving one proposal",
    )
    @app.post(
        "/conversations/{conversation_id}/resolution-proposals/{proposal_id}/approval",
        tags=["resolutions"],
        include_in_schema=False,
    )
    @app.post(
        "/conversations/{conversation_id}/proposals/{proposal_id}/approve",
        tags=["resolutions"],
        include_in_schema=False,
    )
    def approve_parent_scoped_resolution_proposal(
        conversation_id: str,
        proposal_id: str,
        user_id: UserDep,
        service: ServiceDep,
        response: Response,
        body: ApproveResolutionProposalRequest | None = None,
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        request_id = body.request_id if body is not None else None
        result, created = service.approve_resolution_proposal(
            proposal_id,
            user_id,
            request_id=request_id,
            expected_parent_conversation_id=conversation_id,
        )
        response.status_code = 201 if created else 200
        return result

    # ------------------------------------------------------------------
    # Notification inbox (claim / confirm)
    # ------------------------------------------------------------------

    @app.post(
        "/notifications/claim",
        response_model=ClaimBatchOut,
        tags=["notifications"],
        summary=(
            "Claim a leased batch of pending notifications; retrying the same "
            "request_id replays the original batch"
        ),
    )
    def claim_notifications(
        body: ClaimRequest, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.claim_notifications(user_id, body.request_id, body.limit)

    @app.post(
        "/notifications/confirm",
        response_model=ConfirmOut,
        tags=["notifications"],
        summary=(
            "Confirm one claimed notification with its batch credential "
            "(idempotent)"
        ),
    )
    def confirm_notification(
        body: ConfirmRequest, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.confirm_notification(
            user_id, body.batch_id, body.notification_id
        )

    # ------------------------------------------------------------------
    # Desktop replica sync
    # ------------------------------------------------------------------

    @app.post(
        "/sync/pull",
        response_model=SyncPullResponse,
        tags=["sync"],
        summary=(
            "Pull the next batch of changes for one device's replica; retrying "
            "an old cursor replays that sequence range against a fresh snapshot"
        ),
    )
    def pull_sync(
        body: SyncPullRequest, user_id: UserDep, service: ServiceDep
    ) -> dict[str, Any]:
        service.authenticate(user_id)
        return service.pull_sync(
            user_id, body.device_id, body.cursor, body.limit
        )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    @app.get(
        "/conversations/{conversation_id}/events",
        response_model=list[EventOut],
        tags=["audit"],
        summary="Append-only commit-ordered event log for this conversation",
    )
    def list_events(
        conversation_id: str,
        user_id: UserDep,
        service: ServiceDep,
        after_seq: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    ) -> list[dict[str, Any]]:
        service.authenticate(user_id)
        return service.list_events(conversation_id, user_id, after_seq, limit)

    return app


app = create_app()
