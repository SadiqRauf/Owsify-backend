"""Login, token issuing, refresh-token rotation, and logout."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update as sql_update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AuthenticationError, InvalidCredentialsError
from app.core.security import (
    TokenType,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.password_reset import PasswordResetToken
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


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #


def _hash_reset_token(token: str) -> str:
    """SHA-256, hex. See `models/password_reset.py` for why not bcrypt."""
    return hashlib.sha256(token.encode()).hexdigest()


def request_password_reset(
    db: Session,
    email: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, str] | None:
    """Issue a reset token, or return None when there is nothing to issue one for.

    Returning None rather than raising is deliberate: the endpoint must answer
    identically whether or not the address has an account, so "no such user" cannot
    be a distinguishable outcome at the API boundary. **Account enumeration is the
    real risk in this flow** — a forgot-password form that says "no account found"
    is a free tool for checking which of a leaked address list uses your app.

    Returns the user and the *plain* token, which is emailed and never stored.
    """
    user = user_service.get_by_email(db, email)
    if user is None or not user.is_active:
        return None

    window_start = datetime.now(UTC) - timedelta(
        minutes=settings.PASSWORD_RESET_WINDOW_MINUTES
    )
    recent = (
        db.scalar(
            select(func.count())
            .select_from(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.created_at >= window_start,
            )
        )
        or 0
    )
    if recent >= settings.PASSWORD_RESET_MAX_PER_WINDOW:
        # Treated exactly like an unknown address: silently no-op. Telling the
        # caller they are rate limited would confirm the account exists, which is
        # the one thing this endpoint must not do.
        return None

    # Any older link is invalidated. Someone who asks again is telling you the
    # first link did not reach them, and leaving several live at once widens the
    # window for the wrong person to use one.
    db.execute(
        sql_update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=datetime.now(UTC))
    )

    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            token_hash=_hash_reset_token(token),
            user_id=user.id,
            expires_at=datetime.now(UTC)
            + timedelta(minutes=settings.PASSWORD_RESET_EXPIRE_MINUTES),
            requested_ip=ip,
            user_agent=user_agent,
        )
    )
    db.commit()

    return user, token


def _usable_reset_token(db: Session, token: str) -> PasswordResetToken:
    stored = db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _hash_reset_token(token)
        )
    )
    # Expired, already used, and never existed are one message on purpose: telling
    # them apart tells an attacker which guesses were once real tokens.
    if stored is None or not stored.is_usable:
        raise AuthenticationError("This reset link is invalid or has expired.")
    return stored


def check_reset_token(db: Session, token: str) -> User:
    """Validate without consuming, so a reset page can fail before asking for input.

    Making someone type a new password twice only to be told the link died is a bad
    trade for one cheap request.
    """
    return _usable_reset_token(db, token).user


def reset_password(db: Session, token: str, new_password: str) -> User:
    """Set the new password, burn the token, and end every existing session.

    Signing other sessions out is the point of the flow as much as the new password
    is: a reset usually follows either a forgotten password or a suspected
    compromise, and leaving the attacker's refresh token alive would make the reset
    theatre.
    """
    stored = _usable_reset_token(db, token)
    user = stored.user

    user.hashed_password = hash_password(new_password)
    stored.used_at = datetime.now(UTC)

    revoke_all_for_user(db, user.id)
    db.commit()
    db.refresh(user)
    return user
