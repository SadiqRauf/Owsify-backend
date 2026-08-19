"""User profile endpoints."""

import uuid

from fastapi import APIRouter

from app.api.deps import ActiveUser, DbSession
from app.schemas.common import Message
from app.schemas.user import PasswordChange, UserRead, UserUpdate
from app.services import auth as auth_service
from app.services import user as user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead, summary="Read your profile")
def read_own_profile(current_user: ActiveUser) -> UserRead:
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead, summary="Update your profile")
def update_own_profile(
    payload: UserUpdate, db: DbSession, current_user: ActiveUser
) -> UserRead:
    user = user_service.update(db, current_user, payload)
    return UserRead.model_validate(user)


@router.post("/me/password", response_model=Message, summary="Change your password")
def change_own_password(
    payload: PasswordChange, db: DbSession, current_user: ActiveUser
) -> Message:
    user_service.change_password(db, current_user, payload)
    # Every existing session was issued against the old password, so drop them all.
    auth_service.revoke_all_for_user(db, current_user.id)
    return Message(message="Password updated. Please sign in again.")


@router.get("/{user_id}", response_model=UserRead, summary="Read a user by id")
def read_user(user_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> UserRead:
    """Public profile of another user, used later for group members and invites."""
    user = user_service.get_or_404(db, user_id)
    return UserRead.model_validate(user)
