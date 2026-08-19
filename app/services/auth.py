"""Login, token issuing, refresh-token rotation, and logout."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update as sql_update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AuthenticationError, InvalidCredentialsError
from app.core.security import (
    TokenType,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import AuthResponse
from app.schemas.user import UserRead
from app.services import user as user_service


def authenticate(db: Session, email: str, password: str) -> User:
    user = user_service.get_by_email(db, email)

    # Hash a dummy password when the user is missing so that a wrong email and a
    # wrong password take the same amount of time.
    if user is None:
        verify_password(password, "$2b$12$" + "x" * 53)
        raise InvalidCredentialsError()

    if not verify_password(password, user.hashed_password):
        raise InvalidCredentialsError()

    if not user.is_active:
        raise AuthenticationError("This account has been deactivated.")

    return user


def issue_tokens(db: Session, user: User, *, user_agent: str | None = None) -> AuthResponse:
    """Mint an access/refresh pair and record the refresh token's jti."""
    access_token, _ = create_access_token(user.id)
    refresh_token, jti, refresh_expires_at = create_refresh_token(user.id)

    db.add(
        RefreshToken(
            jti=jti,
            user_id=user.id,
            expires_at=refresh_expires_at,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    user.last_login_at = datetime.now(UTC)
    db.commit()
    db.refresh(user)

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserRead.model_validate(user),
    )


def refresh(db: Session, token: str, *, user_agent: str | None = None) -> AuthResponse:
    """Exchange a refresh token for a new pair, revoking the one presented."""
    try:
        payload = decode_token(token, TokenType.REFRESH)
    except TokenError as exc:
        raise AuthenticationError(str(exc)) from exc

    stored = db.scalar(select(RefreshToken).where(RefreshToken.jti == payload.jti))
    if stored is None:
        raise AuthenticationError("Refresh token is no longer valid.")

    if not stored.is_usable:
        # A revoked token being replayed may mean it was stolen, so drop every
        # session this user has rather than just refusing the one request.
        revoke_all_for_user(db, stored.user_id)
        raise AuthenticationError("Refresh token has been revoked.")

    user = user_service.get_by_id(db, uuid.UUID(payload.subject))
    if user is None or not user.is_active:
        raise AuthenticationError("Account is unavailable.")

    stored.revoke()
    db.commit()

    return issue_tokens(db, user, user_agent=user_agent)


def logout(db: Session, user: User, token: str | None) -> int:
    """Revoke one session, or all of them when no token is supplied."""
    if token is None:
        return revoke_all_for_user(db, user.id)

    try:
        payload = decode_token(token, TokenType.REFRESH)
    except TokenError:
        # Already expired or garbage — nothing left to revoke, so this is a no-op.
        return 0

    stored = db.scalar(
        select(RefreshToken).where(
            RefreshToken.jti == payload.jti,
            RefreshToken.user_id == user.id,
        )
    )
    if stored is None or stored.revoked:
        return 0

    stored.revoke()
    db.commit()
    return 1


def revoke_all_for_user(db: Session, user_id: uuid.UUID) -> int:
    result = db.execute(
        sql_update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))
        .values(revoked=True, revoked_at=datetime.now(UTC))
    )
    db.commit()
    return result.rowcount or 0
