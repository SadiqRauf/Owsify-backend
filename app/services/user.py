"""User persistence and profile operations."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    BadRequestError,
    EmailAlreadyRegisteredError,
    NotFoundError,
)
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.schemas.user import PasswordChange, UserCreate, UserUpdate


def get_by_id(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.lower()))


def get_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = get_by_id(db, user_id)
    if user is None:
        raise NotFoundError("User not found.")
    return user


def create(db: Session, payload: UserCreate) -> User:
    email = payload.email.lower()
    if get_by_email(db, email) is not None:
        raise EmailAlreadyRegisteredError()

    user = User(
        email=email,
        full_name=payload.full_name,
        currency=payload.currency,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def update(db: Session, user: User, payload: UserUpdate) -> User:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


def change_password(db: Session, user: User, payload: PasswordChange) -> User:
    if not verify_password(payload.current_password, user.hashed_password):
        raise BadRequestError("Current password is incorrect.")
    if verify_password(payload.new_password, user.hashed_password):
        raise BadRequestError("New password must differ from the current one.")

    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    db.refresh(user)
    return user
