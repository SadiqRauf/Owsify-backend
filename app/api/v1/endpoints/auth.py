"""Registration, login, token refresh, logout, password reset, and /me."""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import ActiveUser, CurrentUser, DbSession
from app.core.email import Email, send_email
from app.core.exceptions import AppError
from app.schemas.auth import (
    AuthResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    ResetPasswordRequest,
    ResetTokenCheck,
    TokenPair,
)
from app.schemas.common import Message
from app.schemas.user import UserCreate, UserRead
from app.services import auth as auth_service
from app.services import invitation as invitation_service
from app.services import password_reset_email
from app.services import user as user_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and sign in",
)
def register(payload: UserCreate, db: DbSession, request: Request) -> AuthResponse:
    """Any open email invitations for this address become friendships immediately."""
    user = user_service.create(db, payload)

    try:
        invitation_service.redeem_for_new_user(db, user)
    except Exception:
        # A signup must never fail because of invitation bookkeeping; the invites
        # stay open and can be redeemed by a later friend request instead.
        logger.exception("Could not redeem invitations for %s", user.email)
        db.rollback()

    return auth_service.issue_tokens(db, user, user_agent=_user_agent(request))


@router.post("/login", response_model=AuthResponse, summary="Sign in with email and password")
def login(payload: LoginRequest, db: DbSession, request: Request) -> AuthResponse:
    user = auth_service.authenticate(db, payload.email, payload.password)
    return auth_service.issue_tokens(db, user, user_agent=_user_agent(request))


@router.post(
    "/token",
    response_model=TokenPair,
    summary="OAuth2 password flow (powers the Authorize button in these docs)",
)
def login_form(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DbSession,
    request: Request,
) -> TokenPair:
    """Same as `/auth/login`, but takes form data so Swagger UI can authenticate.

    Send the email address in the `username` field.
    """
    user = auth_service.authenticate(db, form_data.username, form_data.password)
    return auth_service.issue_tokens(db, user, user_agent=_user_agent(request))


@router.post("/refresh", response_model=AuthResponse, summary="Exchange a refresh token")
def refresh(payload: RefreshRequest, db: DbSession, request: Request) -> AuthResponse:
    """Rotate the refresh token: the one presented is revoked and a new pair issued."""
    return auth_service.refresh(db, payload.refresh_token, user_agent=_user_agent(request))


@router.get("/me", response_model=UserRead, summary="The signed-in user")
def read_me(current_user: ActiveUser) -> UserRead:
    return UserRead.model_validate(current_user)


@router.post("/logout", response_model=Message, summary="Revoke one or all sessions")
def logout(payload: LogoutRequest, db: DbSession, current_user: CurrentUser) -> Message:
    revoked = auth_service.logout(db, current_user, payload.refresh_token)
    if payload.refresh_token is None:
        return Message(message=f"Signed out of {revoked} session(s).")
    return Message(message="Signed out.")


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #

# One constant, because every path through the endpoint returns it — unknown
# address, rate-limited, mail server down, or success. Building it in a single
# place is what stops a later edit from making one branch distinguishable.
_RESET_SENT = "If that email has an account, a reset link is on its way."


def _deliver_reset_email(email: Email, user_id: uuid.UUID) -> None:
    """Runs after the response has gone out. A failure is logged, never surfaced.

    Surfacing it would turn a mail outage into a way of telling a real address from
    a fake one, which is the one thing this endpoint must not do.
    """
    if not send_email(email):
        logger.error("Password reset email could not be sent to user %s", user_id)


@router.post(
    "/forgot-password",
    response_model=Message,
    summary="Send a password reset link",
)
def forgot_password(
    payload: ForgotPasswordRequest,
    db: DbSession,
    request: Request,
    background: BackgroundTasks,
) -> Message:
    """Always answers the same way, whether or not the address has an account.

    That is the security design of this endpoint. A form that says "no account
    found" is a free tool for testing which of a leaked address list uses this app,
    so unknown addresses, rate-limited addresses and successful sends are all
    indistinguishable from outside: same body, same status.

    **The mail is sent after the response, not before it.** Sending inline made the
    endpoint answer in ~20s for a real address and ~40ms for one with no account —
    a timing oracle that gave away exactly what the identical wording was hiding.
    The uniform answer only holds if the work behind it is uniform too.

    The link expires and can be used once. Asking for a new one invalidates any
    earlier link, so a stale message sitting in an inbox stops working.
    """
    issued = auth_service.request_password_reset(
        db,
        payload.email,
        ip=request.client.host if request.client else None,
        user_agent=_user_agent(request),
    )

    if issued is not None:
        user, token = issued
        # Composed here, while the session is still open, so the background task
        # touches no ORM object after the request has ended.
        background.add_task(
            _deliver_reset_email, password_reset_email.build(user, token), user.id
        )

    return Message(message=_RESET_SENT)


@router.get(
    "/reset-password",
    response_model=ResetTokenCheck,
    summary="Check a reset link before using it",
)
def check_reset_token(token: str, db: DbSession) -> ResetTokenCheck:
    """Lets the reset page fail early rather than after two password fields.

    Checking does not consume the token. The address comes back only for a token
    that is already valid, so this cannot be used to look one up without holding a
    live link.
    """
    try:
        user = auth_service.check_reset_token(db, token)
    except AppError:
        return ResetTokenCheck(valid=False)

    return ResetTokenCheck(valid=True, email=user.email)


@router.post(
    "/reset-password",
    response_model=Message,
    summary="Set a new password from a reset link",
)
def reset_password(payload: ResetPasswordRequest, db: DbSession) -> Message:
    """Sets the password, burns the link, and signs every session out.

    Ending other sessions matters as much as the new password does: a reset often
    follows a suspected compromise, and leaving the attacker's refresh token alive
    would make the whole exercise theatre. Everyone signs in again, including the
    person doing the reset.
    """
    auth_service.reset_password(db, payload.token, payload.new_password)
    return Message(
        message="Your password has been changed. Sign in with your new password."
    )
