"""One person, across every way you owe each other.

The app records money between two people in three separate places — group
expenses, settlements against them, and khatas. Each was built for its own view.
This module is the one place that adds them up, and it does so by asking the
existing services rather than re-deriving anything: if the person page and the
group page ever disagreed about a group balance, the person page would be the one
people stopped trusting.

All three components are derived, never stored: group balances from the balance
engine, khata balances from their entries, loan balances from their payments. The
person page therefore cannot drift out of step with the pages it aggregates.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.expense import Expense, ExpenseSplit
from app.models.group import Group, GroupMember
from app.models.khata import KhataAccount, KhataEntry
from app.models.loan import Loan, LoanPayment
from app.models.settlement import Settlement
from app.models.user import User
from app.services import balance as balance_service
from app.services import loan as loan_service
from app.services.khata_entry import signed_amount_sql

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class PersonSummary:
    person: User
    currency: str

    group_balance: Decimal
    """Net from group expenses, already net of settlements — the figure the group
    pages show, so the two can never disagree."""

    khata_balance: Decimal
    loan_balance: Decimal
    total_balance: Decimal

    settled_total: Decimal
    """Gross settled between you, for context. Not part of the total: it has
    already been applied to `group_balance`."""

    shared_group_count: int
    khata_count: int
    loan_count: int
    expense_count: int
    khata_ids: list[uuid.UUID] = field(default_factory=list)

    available_currencies: list[str] = field(default_factory=list)
    """Currencies this pair has money recorded in, most active first.

    Without this a page defaults to the viewer's profile currency and can announce
    "Settled up" for someone who owes a fortune in another one — a false statement,
    not merely an empty view. Not padded with the viewer's currency, so an empty
    list genuinely means nothing is recorded between them.
    """


def get_person_or_404(db: Session, user_id: uuid.UUID) -> User:
    person = db.get(User, user_id)
    if person is None or not person.is_active:
        raise NotFoundError("Person not found.")
    return person


def _khata_balance(
    db: Session, owner_id: uuid.UUID, person_user_id: uuid.UUID, currency: str
) -> tuple[Decimal, int, list[uuid.UUID]]:
    """Summed across every khata you keep for this person in this currency.

    Normally there is at most one — the unique index sees to that — but the sum is
    written to handle more, since archiving and re-opening could leave several.
    """
    khatas = list(
        db.scalars(
            select(KhataAccount).where(
                KhataAccount.owner_id == owner_id,
                KhataAccount.person_user_id == person_user_id,
                KhataAccount.currency == currency,
            )
        )
    )
    if not khatas:
        return ZERO, 0, []

    ids = [khata.id for khata in khatas]
    balance = db.scalar(
        select(func.coalesce(func.sum(signed_amount_sql()), 0)).where(
            KhataEntry.khata_id.in_(ids)
        )
    )
    return Decimal(balance or 0).quantize(Decimal("0.01")), len(khatas), ids


def pair_currencies(db: Session, viewer: User, person_id: uuid.UUID) -> list[str]:
    """Currencies in which these two have anything recorded, most active first."""
    counts: dict[str, int] = {}

    for currency, count in db.execute(
        select(KhataAccount.currency, func.count())
        .join(KhataEntry, KhataEntry.khata_id == KhataAccount.id)
        .where(KhataAccount.owner_id == viewer.id, KhataAccount.person_user_id == person_id)
        .group_by(KhataAccount.currency)
    ).all():
        counts[currency] = counts.get(currency, 0) + count

    for currency, count in db.execute(
        select(Loan.currency, func.count())
        .where(Loan.owner_id == viewer.id, Loan.counterparty_user_id == person_id)
        .group_by(Loan.currency)
    ).all():
        counts[currency] = counts.get(currency, 0) + count

    for currency, count in db.execute(
        select(Expense.currency, func.count(func.distinct(Expense.id)))
        .join(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
        .where(
            ExpenseSplit.user_id == person_id,
            Expense.id.in_(
                select(ExpenseSplit.expense_id).where(ExpenseSplit.user_id == viewer.id)
            ),
        )
        .group_by(Expense.currency)
    ).all():
        counts[currency] = counts.get(currency, 0) + count

    for currency, count in db.execute(
        select(Settlement.currency, func.count())
        .where(
            or_(
                (Settlement.from_user_id == viewer.id) & (Settlement.to_user_id == person_id),
                (Settlement.from_user_id == person_id) & (Settlement.to_user_id == viewer.id),
            )
        )
        .group_by(Settlement.currency)
    ).all():
        counts[currency] = counts.get(currency, 0) + count

    return [code for code, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def summarise(
    db: Session,
    viewer: User,
    person_id: uuid.UUID,
    *,
    currency: str | None = None,
    ledger: balance_service.Ledger | None = None,
    with_counts: bool = True,
) -> PersonSummary:
    person = get_person_or_404(db, person_id)
    code = currency or viewer.currency

    # Group side: taken from the balance engine so this page and the group pages
    # are answering with the same arithmetic, not two implementations of it. The
    # caller may pass a ledger in: the people list summarises everyone at once,
    # and rebuilding the whole thing per row would be quadratic for no gain.
    if ledger is None:
        ledger = balance_service.build_ledger(db, viewer_id=viewer.id)
    group_balance = balance_service.from_cents(ledger.between(viewer.id, person_id, code))

    settled_total = Decimal(
        db.scalar(
            select(func.coalesce(func.sum(Settlement.amount), 0)).where(
                Settlement.currency == code,
                or_(
                    (Settlement.from_user_id == viewer.id)
                    & (Settlement.to_user_id == person_id),
                    (Settlement.from_user_id == person_id)
                    & (Settlement.to_user_id == viewer.id),
                ),
            )
        )
        or 0
    ).quantize(Decimal("0.01"))

    khata_balance, khata_count, khata_ids = _khata_balance(db, viewer.id, person_id, code)

    # The counts are for the person page's supporting copy, not the balance. The
    # people list asks for one summary per contact and shows none of them, so it
    # opts out rather than paying for two extra queries per row.
    shared_groups = 0
    expense_count = 0

    if with_counts:
        shared_groups = (
            db.scalar(
                select(func.count())
                .select_from(GroupMember)
                .where(
                    GroupMember.user_id == person_id,
                    GroupMember.group_id.in_(
                        select(GroupMember.group_id).where(GroupMember.user_id == viewer.id)
                    ),
                )
            )
            or 0
        )

        expense_count = (
            db.scalar(
                select(func.count(func.distinct(Expense.id)))
                .select_from(Expense)
                .join(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
                .where(
                    Expense.currency == code,
                    Expense.id.in_(
                        select(ExpenseSplit.expense_id).where(ExpenseSplit.user_id == viewer.id)
                    ),
                    or_(
                        ExpenseSplit.user_id == person_id,
                        Expense.paid_by_id == person_id,
                    ),
                )
            )
            or 0
        )

    # Net across every loan between you, in either direction and including any
    # overpayment that flipped one. Cancelled loans excluded — a written-off loan is
    # not money anyone expects.
    loan_balance = loan_service.balance_with(db, viewer.id, person_id, code)

    loan_count = (
        db.scalar(
            select(func.count())
            .select_from(Loan)
            .where(
                Loan.owner_id == viewer.id,
                Loan.counterparty_user_id == person_id,
                Loan.currency == code,
                Loan.cancelled_at.is_(None),
            )
        )
        or 0
    )

    return PersonSummary(
        person=person,
        currency=code,
        group_balance=group_balance,
        khata_balance=khata_balance,
        loan_balance=loan_balance,
        total_balance=(group_balance + khata_balance + loan_balance).quantize(Decimal("0.01")),
        settled_total=settled_total,
        shared_group_count=shared_groups,
        khata_count=khata_count,
        loan_count=loan_count,
        expense_count=expense_count,
        khata_ids=khata_ids,
        available_currencies=pair_currencies(db, viewer, person_id),
    )


def list_people(db: Session, viewer: User, *, search: str | None = None) -> list[User]:
    """Everyone you might owe or be owed by.

    People in your groups, people you have settled with, people your khatas link
    to — and your accepted friends. Friends are included even at a zero balance:
    the page is where you go to *start* splitting with someone, so omitting them
    until money exists would make it useless exactly when it is most wanted.
    """
    from app.models.friendship import Friendship, FriendshipStatus

    in_my_groups = select(GroupMember.user_id).where(
        GroupMember.group_id.in_(
            select(GroupMember.group_id).where(GroupMember.user_id == viewer.id)
        )
    )
    settled_with = select(Settlement.from_user_id).where(Settlement.to_user_id == viewer.id)
    settled_to = select(Settlement.to_user_id).where(Settlement.from_user_id == viewer.id)
    khata_linked = select(KhataAccount.person_user_id).where(
        KhataAccount.owner_id == viewer.id, KhataAccount.person_user_id.isnot(None)
    )
    friends = select(Friendship.requester_id).where(
        Friendship.addressee_id == viewer.id,
        Friendship.status == FriendshipStatus.ACCEPTED,
    )
    friends_of = select(Friendship.addressee_id).where(
        Friendship.requester_id == viewer.id,
        Friendship.status == FriendshipStatus.ACCEPTED,
    )

    conditions = [
        User.id != viewer.id,
        User.is_active.is_(True),
        or_(
            User.id.in_(in_my_groups),
            User.id.in_(settled_with),
            User.id.in_(settled_to),
            User.id.in_(khata_linked),
            User.id.in_(friends),
            User.id.in_(friends_of),
        ),
    ]

    if search:
        term = f"%{search.strip().lower()}%"
        conditions.append(
            or_(func.lower(User.full_name).like(term), func.lower(User.email).like(term))
        )

    return list(db.scalars(select(User).where(*conditions).order_by(User.full_name)))


def unlinked_khatas(db: Session, viewer: User, *, search: str | None = None) -> list[KhataAccount]:
    """Khata contacts with no account.

    They belong on the people page too — from the owner's point of view a customer
    who has never installed anything is still a person they are owed money by — but
    they have no user id, so they cannot have a `/people/{id}` summary. The list
    marks them and points at their khata instead.
    """
    conditions = [
        KhataAccount.owner_id == viewer.id,
        KhataAccount.person_user_id.is_(None),
        KhataAccount.is_archived.is_(False),
    ]
    if search:
        term = f"%{search.strip().lower()}%"
        conditions.append(func.lower(KhataAccount.person_name).like(term))

    return list(
        db.scalars(select(KhataAccount).where(*conditions).order_by(KhataAccount.person_name))
    )


def shared_groups(db: Session, viewer_id: uuid.UUID, person_id: uuid.UUID) -> list[Group]:
    return list(
        db.scalars(
            select(Group)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(
                GroupMember.user_id == person_id,
                Group.id.in_(
                    select(GroupMember.group_id).where(GroupMember.user_id == viewer_id)
                ),
            )
            .order_by(Group.name)
        )
    )


@dataclass(frozen=True, slots=True)
class PersonActivityItem:
    """One thing that happened between you and this person.

    Deliberately flatter than `ActivityItem`: this feed merges three sources that
    have nothing in common but a date and an amount, so the shape is the intersection
    of what they can all answer.
    """

    id: uuid.UUID
    kind: str  # expense | settlement | khata_entry
    occurred_at: datetime
    summary: str
    amount: Decimal
    currency: str
    your_impact: Decimal
    """Positive when this left you owed money, negative when owing."""
    group_id: uuid.UUID | None = None
    group_name: str | None = None
    khata_id: uuid.UUID | None = None


def person_activity(
    db: Session,
    viewer: User,
    person_id: uuid.UUID,
    *,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[PersonActivityItem], int]:
    """Expenses, settlements and khata entries between you two, newest first.

    Merged in Python for the same reason the main feed is: three tables with three
    shapes, bounded by one relationship, so the row count stays small and a readable
    merge beats a UNION nobody can modify safely.
    """
    from app.services import activity as activity_service

    person = get_person_or_404(db, person_id)

    shared, _ = activity_service.feed(
        db, viewer.id, with_user_id=person_id, limit=500, offset=0
    )

    items: list[PersonActivityItem] = [
        PersonActivityItem(
            id=item.id,
            kind=item.type.value,
            occurred_at=item.occurred_at,
            summary=item.summary,
            amount=item.amount,
            currency=item.currency,
            your_impact=item.your_impact,
            group_id=item.group.id if item.group else None,
            group_name=item.group.name if item.group else None,
        )
        for item in shared
    ]

    khata_ids = list(
        db.scalars(
            select(KhataAccount.id).where(
                KhataAccount.owner_id == viewer.id,
                KhataAccount.person_user_id == person_id,
            )
        )
    )

    if khata_ids:
        entries = db.scalars(
            select(KhataEntry).where(KhataEntry.khata_id.in_(khata_ids))
        ).all()
        currencies = {
            khata_id: currency
            for khata_id, currency in db.execute(
                select(KhataAccount.id, KhataAccount.currency).where(
                    KhataAccount.id.in_(khata_ids)
                )
            ).all()
        }

        for entry in entries:
            items.append(
                PersonActivityItem(
                    id=entry.id,
                    kind="khata_entry",
                    occurred_at=entry.created_at,
                    summary=_khata_summary(entry, person.full_name),
                    amount=abs(entry.amount),
                    currency=currencies.get(entry.khata_id, viewer.currency),
                    your_impact=entry.signed_amount,
                    khata_id=entry.khata_id,
                )
            )

    # Ties broken on id so the order is stable across requests — several rows can
    # share a timestamp when they were written in one transaction.
    items.sort(key=lambda item: (item.occurred_at, str(item.id)), reverse=True)
    return items[offset : offset + limit], len(items)


def _khata_summary(entry: KhataEntry, person_name: str) -> str:
    from app.models.khata import KhataEntryType

    note = f" — {entry.description}" if entry.description else ""
    if entry.entry_type is KhataEntryType.GIVEN:
        return f"You gave {person_name}{note}"
    if entry.entry_type is KhataEntryType.RECEIVED:
        return f"{person_name} paid you back{note}"
    return f"Adjustment on {person_name}'s khata{note}"
