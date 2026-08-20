"""Friend requests, the friend list, and user search."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Query, status

from app.api.deps import ActiveUser, DbSession
from app.schemas.common import Message
from app.schemas.friendship import (
    FriendRequestCreate,
    FriendshipRead,
    FriendSummary,
    UserSearchResult,
)
from app.core.config import settings
from app.schemas.invitation import InvitationCreate, InvitationRead
from app.schemas.user import UserRead
from app.services import friendship as friendship_service
from app.services import invitation as invitation_service

router = APIRouter(prefix="/friends", tags=["friends"])


def _delivery_mode() -> str:
    """How outbound mail is currently handled, mirrored to the client verbatim."""
    return {"smtp": "email", "file": "file"}.get(settings.EMAIL_BACKEND, "console")


def _invitation_read(invitation: object) -> InvitationRead:
    """Serialise an invitation and stamp on how mail is currently being handled.

    `delivery` is not a column, so it is set after validation rather than passed
    into model_validate, which takes no update argument.
    """
    return InvitationRead.model_validate(invitation).model_copy(
        update={"delivery": _delivery_mode()}
    )


def _to_read(friendship, viewer_id: uuid.UUID) -> FriendshipRead:
    """Flatten a row into the other person plus which way the request points."""
    return FriendshipRead(
        id=friendship.id,
        status=friendship.status,
        created_at=friendship.created_at,
        responded_at=friendship.responded_at,
        user=UserRead.model_validate(friendship.other_user(viewer_id)),
        is_incoming=friendship.addressee_id == viewer_id,
    )


@router.get("", response_model=list[FriendSummary], summary="List your friends")
def list_friends(db: DbSession, current_user: ActiveUser) -> list[FriendSummary]:
    return [
        FriendSummary(
            friendship_id=friendship.id,
            user=UserRead.model_validate(friendship.other_user(current_user.id)),
            friends_since=friendship.responded_at,
        )
        for friendship in friendship_service.list_friends(db, current_user.id)
    ]


@router.get("/requests", response_model=list[FriendshipRead], summary="Pending requests")
def list_requests(
    db: DbSession,
    current_user: ActiveUser,
    direction: Annotated[Literal["incoming", "outgoing"], Query()] = "incoming",
) -> list[FriendshipRead]:
    requests = friendship_service.list_requests(
        db, current_user.id, incoming=direction == "incoming"
    )
    return [_to_read(request, current_user.id) for request in requests]


@router.post(
    "/requests",
    response_model=FriendshipRead,
    status_code=status.HTTP_201_CREATED,
    summary="Send a friend request",
)
def send_request(
    payload: FriendRequestCreate, db: DbSession, current_user: ActiveUser
) -> FriendshipRead:
    """If the other person already asked you, this accepts their request instead."""
    friendship = friendship_service.send_request(db, current_user, payload)
    return _to_read(friendship, current_user.id)


@router.post(
    "/requests/{friendship_id}/accept",
    response_model=FriendshipRead,
    summary="Accept a request",
)
def accept_request(
    friendship_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> FriendshipRead:
    friendship = friendship_service.get_or_404(db, friendship_id)
    friendship = friendship_service.respond(db, friendship, current_user.id, accept=True)
    return _to_read(friendship, current_user.id)


@router.post(
    "/requests/{friendship_id}/reject",
    response_model=FriendshipRead,
    summary="Reject a request",
)
def reject_request(
    friendship_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> FriendshipRead:
    friendship = friendship_service.get_or_404(db, friendship_id)
    friendship = friendship_service.respond(db, friendship, current_user.id, accept=False)
    return _to_read(friendship, current_user.id)


@router.delete(
    "/requests/{friendship_id}",
    response_model=Message,
    summary="Withdraw a request you sent",
)
def cancel_request(friendship_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    friendship = friendship_service.get_or_404(db, friendship_id)
    friendship_service.cancel_request(db, friendship, current_user.id)
    return Message(message="Request withdrawn.")


# --------------------------------------------------------------------------- #
# Invitations — for people who are not on Owsify yet
# --------------------------------------------------------------------------- #
@router.get(
    "/invitations", response_model=list[InvitationRead], summary="Invitations you have sent"
)
def list_invitations(db: DbSession, current_user: ActiveUser) -> list[InvitationRead]:
    return [
        _invitation_read(invitation)
        for invitation in invitation_service.list_sent(db, current_user.id)
    ]


@router.post(
    "/invitations",
    response_model=InvitationRead,
    status_code=status.HTTP_201_CREATED,
    summary="Invite someone by email",
)
def send_invitation(
    payload: InvitationCreate,
    db: DbSession,
    current_user: ActiveUser,
    background_tasks: BackgroundTasks,
) -> InvitationRead:
    """Email an invite to an address that has no account yet.

    When they register with that address they are connected to you automatically.
    If an account already exists this returns 409 with detail type `account_exists`,
    so the client can offer a friend request instead.
    """
    invitation = invitation_service.create(db, current_user, str(payload.email), payload.message)

    # Sending happens after the response, so a slow mail server never delays the UI.
    background_tasks.add_task(invitation_service.deliver, invitation, current_user)

    return _invitation_read(invitation)


@router.post(
    "/invitations/{invitation_id}/resend",
    response_model=InvitationRead,
    summary="Send an invitation again",
)
def resend_invitation(
    invitation_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    background_tasks: BackgroundTasks,
) -> InvitationRead:
    """Also pushes the expiry out by the configured window."""
    invitation = invitation_service.resend(db, invitation_id, current_user.id)
    background_tasks.add_task(invitation_service.deliver, invitation, current_user)
    return _invitation_read(invitation)


@router.delete(
    "/invitations/{invitation_id}", response_model=Message, summary="Cancel an invitation"
)
def cancel_invitation(invitation_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    invitation_service.cancel(db, invitation_id, current_user.id)
    return Message(message="Invitation cancelled.")


@router.delete("/{user_id}", response_model=Message, summary="Remove a friend")
def remove_friend(user_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    friendship_service.remove_friend(db, current_user.id, user_id)
    return Message(message="Friend removed.")


search_router = APIRouter(prefix="/users", tags=["users"])


@search_router.get(
    "/search",
    response_model=list[UserSearchResult],
    summary="Search people by name or email",
)
def search_users(
    db: DbSession,
    current_user: ActiveUser,
    q: Annotated[str, Query(min_length=2, max_length=120, description="Name or email fragment.")],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> list[UserSearchResult]:
    """Each hit says how you already relate to that person, so the UI can show the
    right action: add, cancel, accept, or nothing."""
    users = friendship_service.search_users(db, current_user, q, limit)
    return [
        UserSearchResult(
            **UserRead.model_validate(user).model_dump(),
            relationship=friendship_service.relationship_label(db, current_user.id, user.id),
        )
        for user in users
    ]
