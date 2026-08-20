"""Khata entries: the lines a khata's balance is made of.

Entries are the source of truth. No balance is ever stored — every figure the API
reports is summed from these rows at read time, so an edited or deleted entry can
never leave a total behind that disagrees with the book.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, UnprocessableEntityError
from app.models.khata import KhataAccount, KhataEntry, KhataEntryType
from app.models.user import User
from app.schemas.khata_entry import KhataEntryCreate, KhataEntryUpdate

ZERO = Decimal("0.00")


def signed_amount_sql():
    """The SQL twin of `KhataEntry.signed_amount`.

    Summing has to happen in the database, so the rule exists twice. A test asserts
    the two agree for every entry type, because a silent divergence here would
    show up as a balance that is wrong in a way nobody can trace.
    """
    return case(
        (KhataEntry.entry_type == KhataEntryType.GIVEN.value, KhataEntry.amount),
        (KhataEntry.entry_type == KhataEntryType.RECEIVED.value, -KhataEntry.amount),
        # An adjustment already carries its sign.
        else_=KhataEntry.amount,
    )


def _owned_khata(db: Session, khata_id: uuid.UUID, owner_id: uuid.UUID) -> KhataAccount:
    khata = db.scalar(
        select(KhataAccount).where(
            KhataAccount.id == khata_id, KhataAccount.owner_id == owner_id
        )
    )
    if khata is None:
        raise NotFoundError("Khata not found.")
    return khata


def get_entry_or_404(
    db: Session, entry_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[KhataEntry, KhataAccount]:
    """An entry and its khata, scoped to the owner.

    Joined to the account rather than checked afterwards, so an entry in someone
    else's khata is indistinguishable from one that does not exist.
    """
    row = db.execute(
        select(KhataEntry, KhataAccount)
        .join(KhataAccount, KhataAccount.id == KhataEntry.khata_id)
        .where(KhataEntry.id == entry_id, KhataAccount.owner_id == owner_id)
    ).first()

    if row is None:
        raise NotFoundError("Entry not found.")
    return row[0], row[1]


def _validate(entry_type: KhataEntryType, amount: Decimal) -> None:
    """The rules the database also enforces, reported as a usable field error.

    Duplicated on purpose: the constraint is the guarantee, this is the message.
    A 500 from a check violation tells the user nothing about which field to fix.
    """
    if amount == 0:
        raise UnprocessableEntityError(
            "An entry needs a non-zero amount.",
            details=[{"field": "amount", "message": "Enter an amount.", "type": "zero_amount"}],
        )

    if entry_type is not KhataEntryType.ADJUSTMENT and amount < 0:
        raise UnprocessableEntityError(
            "Only an adjustment can be negative.",
            details=[
                {
                    "field": "amount",
                    "message": (
                        "Use 'Given' or 'Received' to set the direction, and keep the "
                        "amount positive. Negative amounts are for adjustments."
                    ),
                    "type": "negative_amount",
                }
            ],
        )


def create(
    db: Session, khata_id: uuid.UUID, owner: User, payload: KhataEntryCreate
) -> KhataEntry:
    khata = _owned_khata(db, khata_id, owner.id)
    _validate(payload.entry_type, payload.amount)

    entry = KhataEntry(
        khata_id=khata.id,
        entry_type=payload.entry_type,
        amount=payload.amount,
        description=payload.description,
        entry_date=payload.entry_date,
        created_by_id=owner.id,
    )
    db.add(entry)

    # Touching the account in the same transaction keeps `updated_at` meaningful
    # as "when this khata last changed", which is what the list sorts by. Both
    # writes commit together or neither does.
    khata.updated_at = func.now()

    db.commit()
    db.refresh(entry)
    return entry


def update(
    db: Session, entry: KhataEntry, khata: KhataAccount, payload: KhataEntryUpdate
) -> KhataEntry:
    data = payload.model_dump(exclude_unset=True)

    entry_type = data.get("entry_type", entry.entry_type)
    amount = data.get("amount", entry.amount)
    _validate(entry_type, amount)

    for field, value in data.items():
        setattr(entry, field, value)

    khata.updated_at = func.now()
    db.commit()
    db.refresh(entry)
    return entry


def delete(db: Session, entry: KhataEntry, khata: KhataAccount) -> None:
    db.delete(entry)
    khata.updated_at = func.now()
    db.commit()


def list_for_khata(
    db: Session,
    khata_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    entry_type: KhataEntryType | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[KhataEntry, Decimal]], int, Decimal]:
    """Entries newest-first, each with the balance as it stood after that line.

    The running balance is computed with a window function over the khata's *whole*
    history, then the page is taken from that result. Summing only the rows on the
    page would restart the total at every page boundary, and filtering by date
    would make it restart mid-history — the number has to mean "the balance after
    this entry", not "the balance after this entry among the ones you can see".
    """
    khata = _owned_khata(db, khata_id, owner_id)

    running = (
        select(
            KhataEntry.id.label("entry_id"),
            func.sum(signed_amount_sql())
            .over(
                partition_by=KhataEntry.khata_id,
                # Ties are broken by id so the sequence is stable: several entries
                # commonly share a date, and an unstable order would make the
                # running balance jump around between requests.
                order_by=(KhataEntry.entry_date, KhataEntry.created_at, KhataEntry.id),
            )
            .label("running_balance"),
        )
        .where(KhataEntry.khata_id == khata.id)
        .subquery()
    )

    conditions = [KhataEntry.khata_id == khata.id]
    if entry_type is not None:
        conditions.append(KhataEntry.entry_type == entry_type)
    if start_date is not None:
        conditions.append(KhataEntry.entry_date >= start_date)
    if end_date is not None:
        conditions.append(KhataEntry.entry_date <= end_date)
    if search:
        conditions.append(
            func.lower(func.coalesce(KhataEntry.description, "")).like(
                f"%{search.strip().lower()}%"
            )
        )

    total = db.scalar(select(func.count()).select_from(KhataEntry).where(*conditions)) or 0

    rows = db.execute(
        select(KhataEntry, running.c.running_balance)
        .join(running, running.c.entry_id == KhataEntry.id)
        .where(*conditions)
        .order_by(
            KhataEntry.entry_date.desc(), KhataEntry.created_at.desc(), KhataEntry.id.desc()
        )
        .limit(limit)
        .offset(offset)
    ).all()

    # The khata's overall balance, independent of any filter on the page.
    balance = db.scalar(
        select(func.coalesce(func.sum(signed_amount_sql()), 0)).where(
            KhataEntry.khata_id == khata.id
        )
    )

    return (
        [(row[0], Decimal(row.running_balance).quantize(Decimal("0.01"))) for row in rows],
        total,
        Decimal(balance or 0).quantize(Decimal("0.01")),
    )


def totals_for_khata(db: Session, khata_id: uuid.UUID) -> dict[str, Decimal]:
    """Given, received and adjusted totals — the arithmetic behind the balance."""
    rows = db.execute(
        select(KhataEntry.entry_type, func.coalesce(func.sum(KhataEntry.amount), 0))
        .where(KhataEntry.khata_id == khata_id)
        .group_by(KhataEntry.entry_type)
    ).all()

    totals = {entry_type.value: ZERO for entry_type in KhataEntryType}
    for entry_type, amount in rows:
        key = entry_type.value if hasattr(entry_type, "value") else str(entry_type)
        totals[key] = Decimal(amount).quantize(Decimal("0.01"))
    return totals
