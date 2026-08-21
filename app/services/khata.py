"""Khata accounts: creation, listing, updates, archiving and deletion.

Balances are summed from `khata_entries` on read rather than kept on the account
row, for the same reason group balances are: a stored total drifts from the lines
behind it the moment an entry is edited, and a wrong balance that looks
authoritative is worse than a slow one.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from app.models.khata import KhataAccount, KhataEntry, KhataEntryType
from app.models.user import User
from app.schemas.khata import KhataCreate, KhataUpdate

ZERO = Decimal("0.00")


def _balance_expression():
    """`GIVEN` adds to what they owe, `RECEIVED` subtracts from it."""
    return func.coalesce(
        func.sum(
            case(
                (KhataEntry.entry_type == KhataEntryType.GIVEN.value, KhataEntry.amount),
                else_=-KhataEntry.amount,
            )
        ),
        0,
    )


def _totals_subquery():
    """Per-khata balance, entry count and latest entry date.

    Aggregated in a subquery and joined, rather than by grouping the main query.
    Grouping the outer select breaks the moment an eager-loaded relationship adds
    its own columns — Postgres rightly refuses `users.id` that is neither grouped
    nor aggregated — and one subquery still costs a single pass over the entries.
    """
    return (
        select(
            KhataEntry.khata_id.label("khata_id"),
            _balance_expression().label("balance"),
            func.count(KhataEntry.id).label("entry_count"),
            func.max(KhataEntry.entry_date).label("last_entry_on"),
        )
        .group_by(KhataEntry.khata_id)
        .subquery()
    )


def _select_with_totals():
    """A khata select carrying its derived totals, zero-filled when it has none."""
    totals = _totals_subquery()
    return (
        select(
            KhataAccount,
            func.coalesce(totals.c.balance, 0).label("balance"),
            func.coalesce(totals.c.entry_count, 0).label("entry_count"),
            totals.c.last_entry_on.label("last_entry_on"),
        ).outerjoin(totals, totals.c.khata_id == KhataAccount.id),
        totals,
    )


def get_or_404(db: Session, khata_id: uuid.UUID, owner_id: uuid.UUID) -> tuple[KhataAccount, Decimal, int, object]:
    """One khata plus its totals.

    Scoped to the owner in the query rather than checked afterwards, so another
    user's khata is indistinguishable from one that does not exist — a khata is a
    private book, and confirming its existence leaks who someone deals with.
    """
    statement, _ = _select_with_totals()
    row = db.execute(
        statement.options(joinedload(KhataAccount.person_user)).where(
            KhataAccount.id == khata_id, KhataAccount.owner_id == owner_id
        )
    ).first()

    if row is None:
        raise NotFoundError("Khata not found.")
    return row[0], Decimal(row.balance).quantize(Decimal("0.01")), row.entry_count, row.last_entry_on


def list_for_owner(
    db: Session,
    owner_id: uuid.UUID,
    *,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
    sort: str = "-updated_at",
) -> tuple[list[tuple], int]:
    conditions = [KhataAccount.owner_id == owner_id]

    if not include_archived:
        conditions.append(KhataAccount.is_archived.is_(False))

    if search:
        term = f"%{search.strip().lower()}%"
        # Matched against the name the owner typed, the linked account's name, and
        # the contact details — whichever the reader happens to remember.
        conditions.append(
            or_(
                func.lower(KhataAccount.person_name).like(term),
                func.lower(func.coalesce(KhataAccount.person_phone, "")).like(term),
                func.lower(func.coalesce(KhataAccount.person_email, "")).like(term),
                KhataAccount.person_user_id.in_(
                    select(User.id).where(func.lower(User.full_name).like(term))
                ),
            )
        )

    total = db.scalar(select(func.count()).select_from(KhataAccount).where(*conditions)) or 0

    column = {
        "updated_at": KhataAccount.updated_at,
        "created_at": KhataAccount.created_at,
        "person_name": KhataAccount.person_name,
    }.get(sort.lstrip("-"), KhataAccount.updated_at)
    ordering = column.desc() if sort.startswith("-") else column.asc()

    statement, _ = _select_with_totals()
    rows = db.execute(
        statement.options(joinedload(KhataAccount.person_user))
        .where(*conditions)
        .order_by(ordering)
        .limit(limit)
        .offset(offset)
    ).all()

    return list(rows), total


def currency_totals(db: Session, owner_id: uuid.UUID, *, include_archived: bool = False):
    """Owed / owing / net per currency across all of a user's khatas."""
    conditions = [KhataAccount.owner_id == owner_id]
    if not include_archived:
        conditions.append(KhataAccount.is_archived.is_(False))

    totals = _totals_subquery()
    rows = db.execute(
        select(
            KhataAccount.currency,
            func.coalesce(totals.c.balance, 0).label("balance"),
        )
        .outerjoin(totals, totals.c.khata_id == KhataAccount.id)
        .where(*conditions)
    ).all()

    buckets: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"owed_to_you": ZERO, "you_owe": ZERO, "khata_count": 0}
    )
    for row in rows:
        bucket = buckets[row[0]]
        balance = Decimal(row.balance).quantize(Decimal("0.01"))
        bucket["khata_count"] = int(bucket["khata_count"]) + 1
        if balance > 0:
            bucket["owed_to_you"] = Decimal(bucket["owed_to_you"]) + balance
        elif balance < 0:
            bucket["you_owe"] = Decimal(bucket["you_owe"]) - balance

    return [
        {
            "currency": currency,
            "owed_to_you": values["owed_to_you"],
            "you_owe": values["you_owe"],
            "net": Decimal(values["owed_to_you"]) - Decimal(values["you_owe"]),
            "khata_count": values["khata_count"],
        }
        for currency, values in sorted(buckets.items())
    ]


def create(db: Session, owner: User, payload: KhataCreate) -> KhataAccount:
    person_user: User | None = None

    if payload.person_user_id is not None:
        if payload.person_user_id == owner.id:
            raise BadRequestError("You cannot keep a khata with yourself.")

        person_user = db.get(User, payload.person_user_id)
        if person_user is None or not person_user.is_active:
            raise NotFoundError("That account does not exist.")

        existing = db.scalar(
            select(KhataAccount).where(
                KhataAccount.owner_id == owner.id,
                KhataAccount.person_user_id == payload.person_user_id,
            )
        )
        if existing is not None:
            raise ConflictError(
                f"You already keep a khata for {existing.person_name}.",
                details=[
                    {
                        "field": "person_user_id",
                        "message": "A khata for this person already exists.",
                        "type": "khata_exists",
                    }
                ],
            )

    khata = KhataAccount(
        owner_id=owner.id,
        person_name=payload.person_name,
        person_user_id=payload.person_user_id,
        person_phone=payload.person_phone,
        person_email=str(payload.person_email) if payload.person_email else None,
        currency=payload.currency,
        notes=payload.notes,
    )
    db.add(khata)
    db.commit()
    db.refresh(khata)
    return khata


def update(db: Session, khata: KhataAccount, owner: User, payload: KhataUpdate) -> KhataAccount:
    data = payload.model_dump(exclude_unset=True)

    if "person_user_id" in data and data["person_user_id"] is not None:
        if data["person_user_id"] == owner.id:
            raise BadRequestError("You cannot keep a khata with yourself.")

        person_user = db.get(User, data["person_user_id"])
        if person_user is None or not person_user.is_active:
            raise NotFoundError("That account does not exist.")

        clash = db.scalar(
            select(KhataAccount).where(
                KhataAccount.owner_id == owner.id,
                KhataAccount.person_user_id == data["person_user_id"],
                KhataAccount.id != khata.id,
            )
        )
        if clash is not None:
            raise ConflictError(f"You already keep a khata for {clash.person_name}.")

    if "person_email" in data and data["person_email"] is not None:
        data["person_email"] = str(data["person_email"])

    for field, value in data.items():
        setattr(khata, field, value)

    db.commit()
    db.refresh(khata)
    return khata


def archive(db: Session, khata: KhataAccount, *, archived: bool = True) -> KhataAccount:
    khata.is_archived = archived
    db.commit()
    db.refresh(khata)
    return khata


def delete(db: Session, khata: KhataAccount) -> None:
    """Permanently remove a khata and every entry in it."""
    db.delete(khata)
    db.commit()


def entry_count(db: Session, khata_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(KhataEntry).where(KhataEntry.khata_id == khata_id)
        )
        or 0
    )
