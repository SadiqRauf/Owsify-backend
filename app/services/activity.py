"""The activity feed.

Derived from the expense and settlement tables rather than written to a separate
log table. The trade-off is deliberate:

* A derived feed can never disagree with the data it describes. An append-only log
  drifts the moment an edit or delete is not mirrored into it, and a feed that
  claims something that is no longer true is worse than one that is merely terse.
* The cost is that only current state is visible: a deleted expense leaves the feed
  entirely, and edits show the new values rather than a history of changes.

If per-field change history is ever needed, the right shape is an events table
written in the same transaction as the change — not a second source of truth for
what currently exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.expense import Expense, ExpenseSplit
from app.models.group import Group, GroupMember
from app.models.settlement import Settlement
from app.models.user import User

ZERO = Decimal("0.00")


class ActivityType(StrEnum):
    EXPENSE = "expense"
    SETTLEMENT = "settlement"


@dataclass(frozen=True, slots=True)
class ActivityItem:
    id: uuid.UUID
    type: ActivityType
    occurred_at: datetime
    actor: User
    """Who did the thing: added the expense, or made the payment."""
    summary: str
    """A rendered sentence, e.g. "Sadiq added Dinner"."""
    amount: Decimal
    currency: str
    group: Group | None
    counterparty: User | None
    """For a settlement, the other side. None for an expense."""
    your_impact: Decimal
    """Positive when this left the viewer owed money, negative when owing."""


def _expense_summary(expense: Expense, viewer_id: uuid.UUID) -> str:
    who = "You" if expense.created_by_id == viewer_id else expense.created_by.full_name
    return f"{who} added {expense.description}"


def _settlement_summary(settlement: Settlement, viewer_id: uuid.UUID) -> str:
    payer = "You" if settlement.from_user_id == viewer_id else settlement.from_user.full_name
    payee = "you" if settlement.to_user_id == viewer_id else settlement.to_user.full_name
    return f"{payer} paid {payee}"


def _expense_impact(expense: Expense, viewer_id: uuid.UUID) -> Decimal:
    paid = expense.amount if expense.paid_by_id == viewer_id else ZERO
    return (paid - expense.share_for(viewer_id)).quantize(Decimal("0.01"))


def _settlement_impact(settlement: Settlement, viewer_id: uuid.UUID) -> Decimal:
    if settlement.from_user_id == viewer_id:
        # You paid, so you are owed that much less — your position moves up.
        return settlement.amount
    if settlement.to_user_id == viewer_id:
        return -settlement.amount
    return ZERO


def _visible_expenses(user_id: uuid.UUID):
    in_my_group = Expense.group_id.in_(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    )
    return or_(
        in_my_group,
        Expense.paid_by_id == user_id,
        Expense.id.in_(select(ExpenseSplit.expense_id).where(ExpenseSplit.user_id == user_id)),
    )


def _visible_settlements(user_id: uuid.UUID):
    in_my_group = Settlement.group_id.in_(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    )
    return or_(
        in_my_group,
        Settlement.from_user_id == user_id,
        Settlement.to_user_id == user_id,
    )


def feed(
    db: Session,
    viewer_id: uuid.UUID,
    *,
    group_id: uuid.UUID | None = None,
    with_user_id: uuid.UUID | None = None,
    types: set[ActivityType] | None = None,
    limit: int = 25,
    offset: int = 0,
    order: Literal["asc", "desc"] = "desc",
) -> tuple[list[ActivityItem], int]:
    """Merge expenses and settlements into one ordered feed.

    Both sides are fetched and merged in Python rather than UNIONed in SQL. The two
    tables have different shapes and different eager-loaded relationships, and the
    feed is bounded by what one person can see — so the row counts stay small enough
    that a correct, readable merge beats a clever query.
    """
    wanted = types or {ActivityType.EXPENSE, ActivityType.SETTLEMENT}
    items: list[ActivityItem] = []

    if ActivityType.EXPENSE in wanted:
        conditions = [_visible_expenses(viewer_id)]
        if group_id is not None:
            conditions.append(Expense.group_id == group_id)
        if with_user_id is not None:
            conditions.append(
                or_(
                    Expense.paid_by_id == with_user_id,
                    Expense.id.in_(
                        select(ExpenseSplit.expense_id).where(
                            ExpenseSplit.user_id == with_user_id
                        )
                    ),
                )
            )

        expenses = db.scalars(
            select(Expense)
            .options(selectinload(Expense.splits), selectinload(Expense.group))
            .where(*conditions)
        ).all()

        items.extend(
            ActivityItem(
                id=expense.id,
                type=ActivityType.EXPENSE,
                occurred_at=expense.created_at,
                actor=expense.created_by,
                summary=_expense_summary(expense, viewer_id),
                amount=expense.amount,
                currency=expense.currency,
                group=expense.group,
                counterparty=None,
                your_impact=_expense_impact(expense, viewer_id),
            )
            for expense in expenses
        )

    if ActivityType.SETTLEMENT in wanted:
        conditions = [_visible_settlements(viewer_id)]
        if group_id is not None:
            conditions.append(Settlement.group_id == group_id)
        if with_user_id is not None:
            conditions.append(
                or_(
                    Settlement.from_user_id == with_user_id,
                    Settlement.to_user_id == with_user_id,
                )
            )

        settlements = db.scalars(
            select(Settlement).options(selectinload(Settlement.group)).where(*conditions)
        ).all()

        items.extend(
            ActivityItem(
                id=settlement.id,
                type=ActivityType.SETTLEMENT,
                occurred_at=settlement.created_at,
                actor=settlement.from_user,
                summary=_settlement_summary(settlement, viewer_id),
                amount=settlement.amount,
                currency=settlement.currency,
                group=settlement.group,
                counterparty=(
                    settlement.to_user
                    if settlement.from_user_id == viewer_id
                    else settlement.from_user
                ),
                your_impact=_settlement_impact(settlement, viewer_id),
            )
            for settlement in settlements
        )

    # Timestamp alone is not a total order: Postgres now() is the transaction
    # timestamp, so several rows written in one transaction share it exactly. The
    # id breaks the tie so paging can never repeat or skip an entry, and so the
    # ascending order is a genuine reversal of the descending one.
    items.sort(key=lambda item: (item.occurred_at, str(item.id)), reverse=order == "desc")

    total = len(items)
    return items[offset : offset + limit], total
