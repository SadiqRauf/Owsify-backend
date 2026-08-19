"""Friend requests, the friend list, and user search."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, ConflictError, NotFoundError
from app.models.friendship import Friendship, FriendshipStatus
from app.models.user import User
from app.schemas.friendship import FriendRequestCreate
from app.services import user as user_service


def _pair_clause(a: uuid.UUID, b: uuid.UUID):
    """Matches the row for this pair regardless of who sent the request."""
    return or_(
        (Friendship.requester_id == a) & (Friendship.addressee_id == b),
        (Friendship.requester_id == b) & (Friendship.addressee_id == a),
    )


def get_between(db: Session, a: uuid.UUID, b: uuid.UUID) -> Friendship | None:
    return db.scalar(select(Friendship).where(_pair_clause(a, b)))


def are_friends(db: Session, a: uuid.UUID, b: uuid.UUID) -> bool:
    existing = get_between(db, a, b)
    return existing is not None and existing.status is FriendshipStatus.ACCEPTED


def friend_ids(db: Session, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = db.execute(
        select(Friendship.requester_id, Friendship.addressee_id).where(
            Friendship.status == FriendshipStatus.ACCEPTED,
            or_(Friendship.requester_id == user_id, Friendship.addressee_id == user_id),
        )
    ).all()
    return {requester if requester != user_id else addressee for requester, addressee in rows}


def send_request(db: Session, sender: User, payload: FriendRequestCreate) -> Friendship:
    target = (
        user_service.get_by_id(db, payload.user_id)
        if payload.user_id
        else user_service.get_by_email(db, str(payload.email))
    )
    if target is None or not target.is_active:
        raise NotFoundError("No account matches that person.")

    if target.id == sender.id:
        raise BadRequestError("You cannot add yourself as a friend.")

    existing = get_between(db, sender.id, target.id)
    if existing is not None:
        if existing.status is FriendshipStatus.ACCEPTED:
            raise ConflictError("You are already friends.")

        if existing.status is FriendshipStatus.PENDING:
            if existing.requester_id == sender.id:
                raise ConflictError("You already have a request pending with this person.")
            # They asked first — treat this as accepting rather than a second request.
            return respond(db, existing, sender.id, accept=True)

        # A previously rejected request can be revived by whoever asks next.
        existing.requester_id = sender.id
        existing.addressee_id = target.id
        existing.status = FriendshipStatus.PENDING
        existing.responded_at = None
        db.commit()
        db.refresh(existing)
        return existing

    friendship = Friendship(requester_id=sender.id, addressee_id=target.id)
    db.add(friendship)
    db.commit()
    db.refresh(friendship)
    return friendship


def get_or_404(db: Session, friendship_id: uuid.UUID) -> Friendship:
    friendship = db.get(Friendship, friendship_id)
    if friendship is None:
        raise NotFoundError("Friend request not found.")
    return friendship


def respond(db: Session, friendship: Friendship, user_id: uuid.UUID, *, accept: bool) -> Friendship:
    if friendship.addressee_id != user_id:
        raise BadRequestError("Only the person who received a request can answer it.")
    if friendship.status is not FriendshipStatus.PENDING:
        raise ConflictError("That request has already been answered.")

    friendship.status = FriendshipStatus.ACCEPTED if accept else FriendshipStatus.REJECTED
    friendship.responded_at = datetime.now(UTC)
    db.commit()
    db.refresh(friendship)
    return friendship


def cancel_request(db: Session, friendship: Friendship, user_id: uuid.UUID) -> None:
    """Withdraw a request you sent, before it has been answered."""
    if friendship.requester_id != user_id:
        raise BadRequestError("Only the sender can withdraw a request.")
    if friendship.status is not FriendshipStatus.PENDING:
        raise ConflictError("That request has already been answered.")

    db.delete(friendship)
    db.commit()


def remove_friend(db: Session, user_id: uuid.UUID, other_id: uuid.UUID) -> None:
    friendship = get_between(db, user_id, other_id)
    if friendship is None or friendship.status is not FriendshipStatus.ACCEPTED:
        raise NotFoundError("You are not friends with that person.")

    db.delete(friendship)
    db.commit()


def list_friends(db: Session, user_id: uuid.UUID) -> list[Friendship]:
    return list(
        db.scalars(
            select(Friendship)
            .where(
                Friendship.status == FriendshipStatus.ACCEPTED,
                or_(Friendship.requester_id == user_id, Friendship.addressee_id == user_id),
            )
            .order_by(Friendship.responded_at.desc().nullslast())
        )
    )


def list_requests(db: Session, user_id: uuid.UUID, *, incoming: bool) -> list[Friendship]:
    column = Friendship.addressee_id if incoming else Friendship.requester_id
    return list(
        db.scalars(
            select(Friendship)
            .where(Friendship.status == FriendshipStatus.PENDING, column == user_id)
            .order_by(Friendship.created_at.desc())
        )
    )


def relationship_label(db: Session, viewer_id: uuid.UUID, other_id: uuid.UUID) -> str:
    if viewer_id == other_id:
        return "self"

    existing = get_between(db, viewer_id, other_id)
    if existing is None or existing.status is FriendshipStatus.REJECTED:
        return "none"
    if existing.status is FriendshipStatus.ACCEPTED:
        return "friends"
    return "request_sent" if existing.requester_id == viewer_id else "request_received"


def search_users(db: Session, viewer: User, query: str, limit: int = 20) -> list[User]:
    """Find people by name or email.

    An exact email match always wins, so inviting someone you know the address of
    works even when their name is common.
    """
    term = query.strip()
    if len(term) < 2:
        raise BadRequestError("Enter at least 2 characters to search.")

    pattern = f"%{term.lower()}%"
    exact_email = term.lower()

    statement = (
        select(User)
        .where(
            User.is_active.is_(True),
            User.id != viewer.id,
            or_(
                func.lower(User.email).like(pattern),
                func.lower(User.full_name).like(pattern),
            ),
        )
        .order_by(
            # Exact email first, then name matches, then the rest alphabetically.
            (func.lower(User.email) == exact_email).desc(),
            cast(func.lower(User.full_name).like(f"{term.lower()}%"), String).desc(),
            User.full_name,
        )
        .limit(min(limit, 50))
    )
    return list(db.scalars(statement))
