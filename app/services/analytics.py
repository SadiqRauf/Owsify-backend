"""Dashboard analytics.

Two decisions shape everything here.

**"Spending" means your share, not what you paid.** If you put £300 on your card
for a group dinner and your share was £60, you spent £60 — the other £240 is a debt
someone owes you, not consumption. Reporting the card total as spending would
overstate every generous payer's figures and understate everyone else's.

**One currency per query.** Totals are never summed across currencies, so every
analytics call is scoped to a single currency (the caller's default unless they say
otherwise). A "total spent" that adds USD to EUR is a number with no meaning.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.expense import Expense, ExpenseCategory, ExpenseSplit
from app.models.group import Group, GroupMember
from app.models.settlement import Settlement

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date | None = None
    end: date | None = None

    @property
    def is_open(self) -> bool:
        return self.start is None and self.end is None


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category: ExpenseCategory
    amount: Decimal
    share_of_total: Decimal
    expense_count: int


class Granularity(StrEnum):
    DAILY = "daily"
    MONTHLY = "monthly"


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """One bucket of the spending series."""

    bucket: str
    """`2026-08-20` for a day, `2026-08` for a month."""
    start: date
    """First day the bucket covers, for sorting and labelling."""
    amount: Decimal
    expense_count: int


# How many buckets each granularity shows by default, and the most it will allow.
SERIES_DEFAULTS: dict[Granularity, tuple[int, int]] = {
    Granularity.DAILY: (30, 366),
    Granularity.MONTHLY: (6, 60),
}


@dataclass(frozen=True, slots=True)
class GroupStat:
    group: Group
    total_expenses: Decimal
    your_share: Decimal
    your_net: Decimal
    expense_count: int
    member_count: int


def _my_share_query(user_id: uuid.UUID, currency: str, window: DateRange) -> Select:
    """Rows of (expense, my share) for expenses the user is actually part of.

    Joined through expense_splits rather than filtered by payer, because the figure
    that matters is what this person consumed.
    """
    statement = (
        select(Expense, ExpenseSplit.amount.label("my_share"))
        .join(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
        .where(ExpenseSplit.user_id == user_id, Expense.currency == currency)
    )
    if window.start is not None:
        statement = statement.where(Expense.expense_date >= window.start)
    if window.end is not None:
        statement = statement.where(Expense.expense_date <= window.end)
    return statement


def spending_by_category(
    db: Session,
    user_id: uuid.UUID,
    currency: str,
    window: DateRange,
    *,
    group_id: uuid.UUID | None = None,
) -> list[CategoryTotal]:
    """Your share of spending, grouped by category, largest first."""
    statement = (
        select(
            Expense.category,
            func.coalesce(func.sum(ExpenseSplit.amount), 0).label("total"),
            func.count(Expense.id).label("count"),
        )
        .join(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
        .where(ExpenseSplit.user_id == user_id, Expense.currency == currency)
        .group_by(Expense.category)
    )
    if group_id is not None:
        statement = statement.where(Expense.group_id == group_id)
    if window.start is not None:
        statement = statement.where(Expense.expense_date >= window.start)
    if window.end is not None:
        statement = statement.where(Expense.expense_date <= window.end)

    rows = db.execute(statement).all()
    grand_total = sum((Decimal(row.total) for row in rows), ZERO)

    totals = [
        CategoryTotal(
            category=row.category,
            amount=Decimal(row.total).quantize(Decimal("0.01")),
            share_of_total=(
                (Decimal(row.total) / grand_total * 100).quantize(Decimal("0.1"))
                if grand_total
                else Decimal("0.0")
            ),
            expense_count=row.count,
        )
        for row in rows
        if Decimal(row.total) != ZERO
    ]
    totals.sort(key=lambda item: -item.amount)
    return totals


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _shift_months(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def spending_series(
    db: Session,
    user_id: uuid.UUID,
    currency: str,
    *,
    granularity: Granularity = Granularity.DAILY,
    count: int | None = None,
    group_id: uuid.UUID | None = None,
    today: date | None = None,
) -> list[SeriesPoint]:
    """Your share per day or per month, oldest first.

    Empty buckets come back as zero rather than being omitted. Dropping them would
    compress quiet stretches and make the shape of a period read as busier and more
    even than it was.

    Both granularities share one query and one gap-filling loop, so a daily and a
    monthly chart of the same data can never disagree about the total.
    """
    anchor = today or date.today()
    default_count, max_count = SERIES_DEFAULTS[granularity]
    buckets = min(count or default_count, max_count)

    if granularity is Granularity.DAILY:
        start = anchor - timedelta(days=buckets - 1)
        bucket_expr = func.to_char(Expense.expense_date, "YYYY-MM-DD")
    else:
        start = _shift_months(_month_start(anchor), -(buckets - 1))
        bucket_expr = func.to_char(Expense.expense_date, "YYYY-MM")

    statement = (
        select(
            bucket_expr.label("bucket"),
            func.coalesce(func.sum(ExpenseSplit.amount), 0).label("total"),
            func.count(Expense.id).label("count"),
        )
        .join(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
        .where(
            ExpenseSplit.user_id == user_id,
            Expense.currency == currency,
            Expense.expense_date >= start,
            Expense.expense_date <= anchor,
        )
        .group_by("bucket")
    )
    if group_id is not None:
        statement = statement.where(Expense.group_id == group_id)

    found = {
        row.bucket: (Decimal(row.total).quantize(Decimal("0.01")), row.count)
        for row in db.execute(statement).all()
    }

    series: list[SeriesPoint] = []
    for index in range(buckets):
        if granularity is Granularity.DAILY:
            current = start + timedelta(days=index)
            key = current.isoformat()
        else:
            current = _shift_months(start, index)
            key = f"{current.year:04d}-{current.month:02d}"

        amount, expense_count = found.get(key, (ZERO, 0))
        series.append(
            SeriesPoint(bucket=key, start=current, amount=amount, expense_count=expense_count)
        )
    return series


def group_statistics(
    db: Session, user_id: uuid.UUID, window: DateRange
) -> list[GroupStat]:
    """Per-group totals, your share, and your net position.

    Nets come from the balance engine so this view can never disagree with the
    group's own balance page.
    """
    from app.services import balance as balance_service

    groups = list(
        db.scalars(
            select(Group)
            .options(selectinload(Group.members))
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == user_id)
            .order_by(Group.created_at.desc())
        )
    )

    stats: list[GroupStat] = []
    for group in groups:
        conditions = [Expense.group_id == group.id]
        if window.start is not None:
            conditions.append(Expense.expense_date >= window.start)
        if window.end is not None:
            conditions.append(Expense.expense_date <= window.end)

        total, count = db.execute(
            select(
                func.coalesce(func.sum(Expense.amount), 0),
                func.count(Expense.id),
            ).where(*conditions)
        ).one()

        your_share = db.scalar(
            select(func.coalesce(func.sum(ExpenseSplit.amount), 0))
            .join(Expense, Expense.id == ExpenseSplit.expense_id)
            .where(ExpenseSplit.user_id == user_id, *conditions)
        ) or ZERO

        ledger = balance_service.build_ledger(db, group_id=group.id)

        stats.append(
            GroupStat(
                group=group,
                total_expenses=Decimal(total).quantize(Decimal("0.01")),
                your_share=Decimal(your_share).quantize(Decimal("0.01")),
                your_net=balance_service.from_cents(
                    ledger.net_for(user_id, group.currency)
                ),
                expense_count=count,
                member_count=len(group.members),
            )
        )
    return stats


def totals_for_window(
    db: Session, user_id: uuid.UUID, currency: str, window: DateRange
) -> tuple[Decimal, int]:
    """Your total share and expense count for the window."""
    rows = db.execute(
        _my_share_query(user_id, currency, window).with_only_columns(
            func.coalesce(func.sum(ExpenseSplit.amount), 0),
            func.count(Expense.id),
        )
    ).one()
    return Decimal(rows[0]).quantize(Decimal("0.01")), rows[1]


def recent_expenses(
    db: Session, user_id: uuid.UUID, *, limit: int = 5
) -> list[Expense]:
    in_my_group = Expense.group_id.in_(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    )
    visible = or_(
        in_my_group,
        Expense.paid_by_id == user_id,
        Expense.id.in_(select(ExpenseSplit.expense_id).where(ExpenseSplit.user_id == user_id)),
    )
    return list(
        db.scalars(
            select(Expense)
            .options(selectinload(Expense.splits).joinedload(ExpenseSplit.user))
            .where(visible)
            .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
            .limit(limit)
        )
    )


def recent_settlements(
    db: Session, user_id: uuid.UUID, *, limit: int = 5
) -> list[Settlement]:
    in_my_group = Settlement.group_id.in_(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    )
    visible = or_(
        in_my_group,
        Settlement.from_user_id == user_id,
        Settlement.to_user_id == user_id,
    )
    return list(
        db.scalars(
            select(Settlement)
            .where(visible)
            .order_by(Settlement.settled_on.desc(), Settlement.created_at.desc())
            .limit(limit)
        )
    )


def default_window(days: int = 180, today: date | None = None) -> DateRange:
    anchor = today or date.today()
    return DateRange(start=anchor - timedelta(days=days), end=anchor)


def spending_by_group(
    db: Session, user_id: uuid.UUID, currency: str, window: DateRange
) -> list[tuple[Group, Decimal]]:
    """Your share per group, largest first — the group spending chart."""
    statement = (
        select(
            Group,
            func.coalesce(func.sum(ExpenseSplit.amount), 0).label("total"),
        )
        .join(Expense, Expense.group_id == Group.id)
        .join(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
        .where(ExpenseSplit.user_id == user_id, Expense.currency == currency)
        .group_by(Group.id)
    )
    if window.start is not None:
        statement = statement.where(Expense.expense_date >= window.start)
    if window.end is not None:
        statement = statement.where(Expense.expense_date <= window.end)

    rows = db.execute(statement).all()
    totals = [
        (row[0], Decimal(row.total).quantize(Decimal("0.01")))
        for row in rows
        if Decimal(row.total) != ZERO
    ]
    totals.sort(key=lambda item: -item[1])
    return totals
