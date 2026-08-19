"""Registration, login, token refresh, logout, and the current-user endpoint."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import ActiveUser, CurrentUser, DbSession
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenPair,
)
from app.schemas.common import Message
from app.schemas.user import UserCreate, UserRead
from app.services import auth as auth_service
from app.services import invitation as invitation_service
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
