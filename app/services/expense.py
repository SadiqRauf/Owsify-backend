"""Expense creation, editing, listing, and the balances they produce."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import (
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    UnprocessableEntityError,
)
from app.models.expense import Expense, ExpenseSplit, SplitType
from app.models.group import Group, GroupMember
from app.models.notification import NotificationType
from app.models.user import User
from app.schemas.expense import ExpenseCreate, ExpenseUpdate, SplitParticipant
from app.services import friendship as friendship_service
from app.services import group as group_service
from app.services import notification as notification_service
from app.services.splits import SplitInput, compute_splits

ZERO = Decimal("0.00")


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #
def _visible_to(user_id: uuid.UUID):
    """An expense is visible if you are in its group, or you are on it personally."""
    in_my_group = Expense.group_id.in_(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    )
    i_paid = Expense.paid_by_id == user_id
    i_owe = Expense.id.in_(select(ExpenseSplit.expense_id).where(ExpenseSplit.user_id == user_id))
    return or_(in_my_group, i_paid, i_owe)


def get_or_404(db: Session, expense_id: uuid.UUID, viewer_id: uuid.UUID) -> Expense:
    expense = db.scalar(
        select(Expense)
        .options(selectinload(Expense.splits).joinedload(ExpenseSplit.user))
        .where(Expense.id == expense_id, _visible_to(viewer_id))
    )
    if expense is None:
        raise NotFoundError("Expense not found.")
    return expense


def _require_edit_rights(db: Session, expense: Expense, user: User) -> None:
    """Anyone who created or paid for an expense can change it; so can a group admin."""
    if expense.created_by_id == user.id or expense.paid_by_id == user.id:
        return

    if expense.group_id is not None:
        group = db.get(Group, expense.group_id)
        membership = group.membership_for(user.id) if group else None
        if membership is not None and membership.role.can_manage_members:
            return

    raise PermissionDeniedError(
        "Only the person who added or paid for this expense can change it."
    )


# --------------------------------------------------------------------------- #
# Participant validation
# --------------------------------------------------------------------------- #
def _validate_participants(
    db: Session,
    actor: User,
    group: Group | None,
    paid_by_id: uuid.UUID,
    participant_ids: Sequence[uuid.UUID],
) -> None:
    unique_ids = set(participant_ids)

    users = db.scalars(select(User).where(User.id.in_(unique_ids | {paid_by_id}))).all()
    found = {user.id for user in users}
    missing = (unique_ids | {paid_by_id}) - found
    if missing:
        raise UnprocessableEntityError(
            "One or more people on this expense do not exist.",
            details=[
                {"field": "splits", "message": f"Unknown user {user_id}.", "type": "unknown_user"}
                for user_id in sorted(missing, key=str)
            ],
        )

    if group is not None:
        member_ids = {member.user_id for member in group.members}
        outsiders = (unique_ids | {paid_by_id}) - member_ids
        if outsiders:
            raise UnprocessableEntityError(
                "Everyone on a group expense must be a member of the group.",
                details=[
                    {
                        "field": "splits",
                        "message": f"User {user_id} is not in this group.",
                        "type": "not_a_member",
                    }
                    for user_id in sorted(outsiders, key=str)
                ],
            )
        return

    # No group: this is a personal expense, so everyone on it must be the actor or
    # one of their friends. Otherwise anyone could push debt onto a stranger.
    allowed = friendship_service.friend_ids(db, actor.id) | {actor.id}
    strangers = (unique_ids | {paid_by_id}) - allowed
    if strangers:
        raise UnprocessableEntityError(
            "You can only share an expense with your friends.",
            details=[
                {
                    "field": "splits",
                    "message": f"User {user_id} is not your friend.",
                    "type": "not_a_friend",
                }
                for user_id in sorted(strangers, key=str)
            ],
        )


def _build_splits(
    amount: Decimal, split_type: SplitType, participants: list[SplitParticipant]
) -> list[ExpenseSplit]:
    computed = compute_splits(
        amount,
        split_type,
        [SplitInput(user_id=p.user_id, value=p.value) for p in participants],
    )
    return [
        ExpenseSplit(user_id=split.user_id, amount=split.amount, percentage=split.percentage)
        for split in computed
    ]


def _replace_splits(db: Session, expense: Expense, new_splits: list[ExpenseSplit]) -> None:
    """Swap an expense's splits, deleting the old rows before inserting the new ones.

    Assigning the collection in one go lets SQLAlchemy order the INSERTs before the
    DELETEs, which trips the (expense_id, user_id) unique constraint whenever a
    participant is carried over. Flushing the removal first avoids that.
    """
    expense.splits.clear()
    db.flush()
    expense.splits.extend(new_splits)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _people_on(expense: Expense) -> set[uuid.UUID]:
    """Everyone whose balance this expense touches: the payer and every split."""
    return {expense.paid_by_id} | {split.user_id for split in expense.splits}


def _positions(expense: Expense) -> dict[uuid.UUID, tuple[bool, Decimal]]:
    """Per person: did they pay, and what is their share. What an edit is judged by."""
    return {
        user_id: (user_id == expense.paid_by_id, expense.share_for(user_id))
        for user_id in _people_on(expense)
    }


def create(db: Session, actor: User, payload: ExpenseCreate) -> Expense:
    group: Group | None = None
    if payload.group_id is not None:
        group = group_service.get_or_404(db, payload.group_id)
        group_service.require_membership(group, actor.id)

    participant_ids = [split.user_id for split in payload.splits]
    _validate_participants(db, actor, group, payload.paid_by_id, participant_ids)

    expense = Expense(
        group_id=payload.group_id,
        description=payload.description,
        amount=payload.amount,
        currency=payload.currency if group is None else group.currency,
        expense_date=payload.expense_date,
        category=payload.category,
        notes=payload.notes,
        split_type=payload.split_type,
        paid_by_id=payload.paid_by_id,
        created_by_id=actor.id,
    )
    expense.splits = _build_splits(payload.amount, payload.split_type, payload.splits)

    db.add(expense)
    db.flush()
    for user_id in _people_on(expense):
        notification_service.notify(
            db,
            user_id,
            actor_id=actor.id,
            type=NotificationType.EXPENSE_ADDED,
            data=notification_service.expense_data(expense, user_id),
        )
    db.commit()
    db.refresh(expense)
    return expense


def update(db: Session, expense: Expense, actor: User, payload: ExpenseUpdate) -> Expense:
    _require_edit_rights(db, expense, actor)
    before = _positions(expense)

    data = payload.model_dump(exclude_unset=True)
    split_participants = payload.splits
    split_type = payload.split_type or expense.split_type
    new_amount = payload.amount if payload.amount is not None else expense.amount
    new_paid_by = payload.paid_by_id or expense.paid_by_id

    group = db.get(Group, expense.group_id) if expense.group_id else None

    if split_participants is not None or payload.paid_by_id is not None:
        participant_ids = (
            [split.user_id for split in split_participants]
            if split_participants is not None
            else [split.user_id for split in expense.splits]
        )
        _validate_participants(db, actor, group, new_paid_by, participant_ids)

    # Checked before anything is assigned. Raising after mutating would leave the
    # session holding a dirty expense that a later flush still tries to write, and
    # the rejection would have quietly changed the object it rejected.
    if (
        split_participants is None
        and split_type is SplitType.EXACT
        and payload.amount is not None
    ):
        raise BadRequestError(
            "Changing the amount of an exact split needs new split amounts too."
        )

    for field in ("description", "amount", "currency", "expense_date", "category", "notes", "paid_by_id"):
        if field in data:
            setattr(expense, field, data[field])

    if payload.split_type is not None:
        expense.split_type = payload.split_type

    # Recompute whenever anything the shares depend on moved.
    if split_participants is not None:
        _replace_splits(db, expense, _build_splits(new_amount, split_type, split_participants))
    elif payload.amount is not None or payload.split_type is not None:
        carried = [
            SplitParticipant(
                user_id=split.user_id,
                value=split.percentage if split_type is SplitType.PERCENTAGE else split.amount,
            )
            for split in expense.splits
        ]
        _replace_splits(db, expense, _build_splits(new_amount, split_type, carried))

    db.flush()
    after = _positions(expense)
    # Only the people whose money moved. Fixing a typo in the description is not
    # worth interrupting anyone for.
    for user_id in before.keys() | after.keys():
        if before.get(user_id) != after.get(user_id):
            notification_service.notify(
                db,
                user_id,
                actor_id=actor.id,
                type=NotificationType.EXPENSE_UPDATED,
                data=notification_service.expense_data(expense, user_id),
            )
    db.commit()
    db.refresh(expense)
    return expense


def delete(db: Session, expense: Expense, actor: User) -> None:
    _require_edit_rights(db, expense, actor)
    for user_id in _people_on(expense):
        notification_service.notify(
            db,
            user_id,
            actor_id=actor.id,
            type=NotificationType.EXPENSE_DELETED,
            data=notification_service.expense_data(expense, user_id),
        )
    db.delete(expense)
    db.commit()


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def list_for_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    group_id: uuid.UUID | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[Expense], int]:
    conditions = [_visible_to(user_id)]
    if group_id is not None:
        conditions.append(Expense.group_id == group_id)

    total = db.scalar(select(func.count()).select_from(Expense).where(*conditions)) or 0

    items = list(
        db.scalars(
            select(Expense)
            .options(selectinload(Expense.splits).joinedload(ExpenseSplit.user))
            .where(*conditions)
            .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def net_for(expense: Expense, user_id: uuid.UUID) -> Decimal:
    """Positive when this expense leaves the user owed money."""
    paid = expense.amount if expense.paid_by_id == user_id else ZERO
    return (paid - expense.share_for(user_id)).quantize(Decimal("0.01"))
