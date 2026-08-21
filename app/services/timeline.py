"""One person's whole history, in one list.

The timeline merges six sources: group expenses, settlements, khata entries, loans
given, loan payments, and notes. They share almost nothing — different tables,
different shapes, some dated by a calendar date and some by a timestamp — so the
merge happens in Python over a bounded set of rows rather than as a six-way UNION
nobody could safely change.

**Ordering is by calendar date first, timestamp second.** A khata entry dated last
Tuesday belongs on last Tuesday even though it was typed in today; the timestamp
only breaks ties within a day. Sorting purely by `created_at` would produce a
timeline that reads as a data-entry log rather than a history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.khata import KhataAccount, KhataEntry, KhataEntryType
from app.models.loan import Loan, LoanDirection, LoanPayment
from app.models.note import Note
from app.models.user import User
from app.services import activity as activity_service


@dataclass(frozen=True, slots=True)
class TimelineItem:
    id: uuid.UUID
    kind: str
    occurred_on: date
    occurred_at: datetime
    title: str
    detail: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    href: str | None = None


def _khata_title(entry: KhataEntry, person_name: str) -> str:
    if entry.entry_type is KhataEntryType.GIVEN:
        return f"Gave {person_name}"
    if entry.entry_type is KhataEntryType.RECEIVED:
        return f"{person_name} paid back"
    return "Khata adjustment"


def for_person(
    db: Session,
    viewer: User,
    person_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TimelineItem], int]:
    """Everything between you and this person, newest first."""
    items: list[TimelineItem] = []

    # --- Expenses and settlements, via the existing feed ---------------------
    shared, _ = activity_service.feed(
        db, viewer.id, with_user_id=person_id, limit=500, offset=0
    )
    for entry in shared:
        items.append(
            TimelineItem(
                id=entry.id,
                kind=entry.type.value,
                occurred_on=entry.occurred_at.date(),
                occurred_at=entry.occurred_at,
                title=entry.summary,
                detail=entry.group.name if entry.group else None,
                amount=entry.amount,
                currency=entry.currency,
                href=(
                    f"/expenses/{entry.id}" if entry.type.value == "expense" else "/settlements"
                ),
            )
        )

    # --- Khata entries ------------------------------------------------------
    khatas = list(
        db.scalars(
            select(KhataAccount).where(
                KhataAccount.owner_id == viewer.id, KhataAccount.person_user_id == person_id
            )
        )
    )
    khata_by_id = {khata.id: khata for khata in khatas}

    if khata_by_id:
        entries = db.scalars(
            select(KhataEntry).where(KhataEntry.khata_id.in_(list(khata_by_id)))
        ).all()
        for entry in entries:
            khata = khata_by_id[entry.khata_id]
            items.append(
                TimelineItem(
                    id=entry.id,
                    kind="khata_entry",
                    occurred_on=entry.entry_date,
                    occurred_at=entry.created_at,
                    title=_khata_title(entry, khata.display_name),
                    detail=entry.description,
                    amount=abs(entry.amount),
                    currency=khata.currency,
                    href=f"/khata/{khata.id}",
                )
            )

    # --- Loans and their payments -------------------------------------------
    loans = list(
        db.scalars(
            select(Loan)
            .options(selectinload(Loan.payments))
            .where(Loan.owner_id == viewer.id, Loan.counterparty_user_id == person_id)
        )
    )

    for loan in loans:
        items.append(
            TimelineItem(
                id=loan.id,
                kind=(
                    "loan_given" if loan.direction is LoanDirection.GIVEN else "loan_taken"
                ),
                # A loan has no separate "given on" field, so the row's creation is
                # the event. Adding one would be a field nobody fills in correctly.
                occurred_on=loan.created_at.date(),
                occurred_at=loan.created_at,
                title=(
                    "Loan given" if loan.direction is LoanDirection.GIVEN else "Loan taken"
                ),
                detail=loan.description,
                amount=loan.amount,
                currency=loan.currency,
                href=f"/loans/{loan.id}",
            )
        )
        for payment in loan.payments:
            items.append(
                TimelineItem(
                    id=payment.id,
                    kind=(
                        "loan_payment"
                        if loan.direction is LoanDirection.GIVEN
                        else "loan_repayment"
                    ),
                    occurred_on=payment.payment_date,
                    occurred_at=payment.created_at,
                    title=(
                        # Named from the record keeper's side: the same row means
                        # money in on a loan you gave and money out on one you took.
                        "Loan payment received"
                        if loan.direction is LoanDirection.GIVEN
                        else "Loan repayment made"
                    ),
                    detail=payment.note,
                    amount=payment.amount,
                    currency=loan.currency,
                    href=f"/loans/{loan.id}",
                )
            )

    # --- Notes --------------------------------------------------------------
    # Notes about the person directly, and notes on any khata or loan of theirs:
    # from a reader's point of view all three are notes about this relationship.
    note_conditions = [Note.person_user_id == person_id]
    if khata_by_id:
        note_conditions.append(Note.khata_id.in_(list(khata_by_id)))
    if loans:
        note_conditions.append(Note.loan_id.in_([loan.id for loan in loans]))

    notes = db.scalars(
        select(Note).where(Note.owner_id == viewer.id, or_(*note_conditions))
    ).all()

    for note in notes:
        items.append(
            TimelineItem(
                id=note.id,
                kind="note",
                occurred_on=note.created_at.date(),
                occurred_at=note.created_at,
                title="Note added",
                detail=note.body,
                href=f"/people/{person_id}",
            )
        )

    # Date first, then timestamp, then id — the id keeps the order stable when the
    # first two tie, which they do for everything written in one transaction.
    items.sort(key=lambda item: (item.occurred_on, item.occurred_at, str(item.id)), reverse=True)

    return items[offset : offset + limit], len(items)
