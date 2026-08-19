"""Friend requests, the friend list, and user search."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.schemas.common import Message
from app.schemas.friendship import (
    FriendRequestCreate,
    FriendshipRead,
    FriendSummary,
    UserSearchResult,
)
from app.schemas.user import UserRead
from app.services import friendship as friendship_service

router = APIRouter(prefix="/friends", tags=["friends"])


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
