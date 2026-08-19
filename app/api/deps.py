"""Shared FastAPI dependencies: database session and the current user."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import TokenError, TokenType, decode_token
from app.db.session import get_db
from app.models.user import User
from app.services import user as user_service

# auto_error=False so a missing header raises our AuthenticationError, keeping the
# response envelope identical to every other failure.
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/token",
    auto_error=False,
)

DbSession = Annotated[Session, Depends(get_db)]
BearerToken = Annotated[str | None, Depends(oauth2_scheme)]


def get_current_user(db: DbSession, token: BearerToken) -> User:
    if not token:
        raise AuthenticationError("Not authenticated.")

    try:
        payload = decode_token(token, TokenType.ACCESS)
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    try:
        user_id = uuid.UUID(payload.subject)
    except ValueError as exc:
        raise AuthenticationError("Token subject is malformed.") from exc

    user = user_service.get_by_id(db, user_id)
    if user is None:
        raise AuthenticationError("The account for this token no longer exists.")

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_current_active_user(current_user: CurrentUser) -> User:
    if not current_user.is_active:
        raise PermissionDeniedError("This account has been deactivated.")
    return current_user


ActiveUser = Annotated[User, Depends(get_current_active_user)]


def get_current_superuser(current_user: ActiveUser) -> User:
    if not current_user.is_superuser:
        raise PermissionDeniedError("This action requires elevated privileges.")
    return current_user


SuperUser = Annotated[User, Depends(get_current_superuser)]


def get_optional_user(db: DbSession, token: BearerToken) -> User | None:
    """For endpoints that behave differently when signed in but do not require it."""
    if not token:
        return None
    try:
        return get_current_user(db, token)
    except AuthenticationError:
        return None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]
