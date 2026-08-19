"""Recording, listing and editing settlements."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from app.models.group import Group
from app.models.settlement import Settlement
from app.models.user import User
from app.schemas.settlement import SettlementCreate, SettlementUpdate
from app.services import balance as balance_service
from app.services import friendship as friendship_service
from app.services import group as group_service


def _visible_to(user_id: uuid.UUID):
    """You can see a settlement if you are on it, or it is in one of your groups."""
    from app.models.group import GroupMember

    in_my_group = Settlement.group_id.in_(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    )
    return or_(
        Settlement.from_user_id == user_id,
        Settlement.to_user_id == user_id,
        in_my_group,
    )


def get_or_404(db: Session, settlement_id: uuid.UUID, viewer_id: uuid.UUID) -> Settlement:
    settlement = db.scalar(
        select(Settlement).where(Settlement.id == settlement_id, _visible_to(viewer_id))
    )
    if settlement is None:
        raise NotFoundError("Settlement not found.")
    return settlement


def create(db: Session, actor: User, payload: SettlementCreate) -> Settlement:
    if payload.from_user_id == payload.to_user_id:
        raise BadRequestError("A settlement needs two different people.")

    group: Group | None = None
    currency = payload.currency

    if payload.group_id is not None:
        group = group_service.get_or_404(db, payload.group_id)
        group_service.require_membership(group, actor.id)

        member_ids = {member.user_id for member in group.members}
        outsiders = {payload.from_user_id, payload.to_user_id} - member_ids
        if outsiders:
            raise UnprocessableEntityError(
                "Both people must be members of the group.",
                details=[
                    {
                        "field": "from_user_id" if user_id == payload.from_user_id else "to_user_id",
                        "message": f"User {user_id} is not in this group.",
                        "type": "not_a_member",
                    }
                    for user_id in sorted(outsiders, key=str)
                ],
            )
        # The group fixes the currency, exactly as it does for expenses.
        currency = group.currency
    else:
        # Outside a group you may only settle with yourself on one side, against a
        # friend — otherwise anyone could fabricate a payment between two strangers.
        if actor.id not in (payload.from_user_id, payload.to_user_id):
            raise PermissionDeniedError(
                "You can only record a settlement that you are part of."
            )
        other_id = (
            payload.to_user_id if payload.from_user_id == actor.id else payload.from_user_id
        )
        if not friendship_service.are_friends(db, actor.id, other_id):
            raise UnprocessableEntityError(
                "You can only settle up with a friend.",
                details=[
                    {
                        "field": "to_user_id",
                        "message": "That person is not your friend.",
                        "type": "not_a_friend",
                    }
                ],
            )

    settlement = Settlement(
        group_id=payload.group_id,
        from_user_id=payload.from_user_id,
        to_user_id=payload.to_user_id,
        amount=payload.amount,
        currency=currency,
        settled_on=payload.settled_on,
        method=payload.method,
        notes=payload.notes,
        created_by_id=actor.id,
    )
    db.add(settlement)
    db.commit()
    db.refresh(settlement)
    return settlement


def _require_edit_rights(settlement: Settlement, user: User) -> None:
    if user.id not in (settlement.created_by_id, settlement.from_user_id, settlement.to_user_id):
        raise PermissionDeniedError(
            "Only someone involved in this settlement can change it."
        )


def update(db: Session, settlement: Settlement, actor: User, payload: SettlementUpdate) -> Settlement:
    _require_edit_rights(settlement, actor)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settlement, field, value)

    db.commit()
    db.refresh(settlement)
    return settlement


def delete(db: Session, settlement: Settlement, actor: User) -> None:
    _require_edit_rights(settlement, actor)
    db.delete(settlement)
    db.commit()


def list_for_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    group_id: uuid.UUID | None = None,
    with_user_id: uuid.UUID | None = None,
    limit: int = 25,
    offset: int = 0,
    sort: str = "-settled_on",
) -> tuple[list[Settlement], int]:
    conditions = [_visible_to(user_id)]

    if group_id is not None:
        conditions.append(Settlement.group_id == group_id)

    if with_user_id is not None:
        conditions.append(
            or_(
                Settlement.from_user_id == with_user_id,
                Settlement.to_user_id == with_user_id,
            )
        )

    total = db.scalar(select(func.count()).select_from(Settlement).where(*conditions)) or 0

    column = {
        "settled_on": Settlement.settled_on,
        "amount": Settlement.amount,
        "created_at": Settlement.created_at,
    }.get(sort.lstrip("-"), Settlement.settled_on)
    ordering = column.desc() if sort.startswith("-") else column.asc()

    items = list(
        db.scalars(
            select(Settlement)
            .where(*conditions)
            .order_by(ordering, Settlement.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def suggest_amount(
    db: Session, user_id: uuid.UUID, other_id: uuid.UUID, *, group_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID, dict[str, int]]:
    """What would fully settle these two, per currency.

    Returns ``(from_user, to_user, {currency: cents})`` from the caller's point of
    view, so a settle-up form can prefill the right direction and amount instead of
    making someone work out the sign themselves.
    """
    ledger = balance_service.build_ledger(db, viewer_id=user_id, group_id=group_id)

    amounts: dict[str, int] = {}
    for currency in ledger.currencies:
        cents = ledger.between(user_id, other_id, currency)
        if cents:
            amounts[currency] = cents

    # Positive means they owe the caller, so they would be the one paying.
    any_positive = any(cents > 0 for cents in amounts.values())
    if any_positive:
        return other_id, user_id, {c: abs(v) for c, v in amounts.items()}
    return user_id, other_id, {c: abs(v) for c, v in amounts.items()}
