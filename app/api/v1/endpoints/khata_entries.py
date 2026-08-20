"""Khata entries — the lines a khata's balance is made of."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.core.exceptions import BadRequestError
from app.models.khata import KhataEntry, KhataEntryType
from app.schemas.common import Message
from app.schemas.khata_entry import (
    KhataEntryCreate,
    KhataEntryListPage,
    KhataEntryRead,
    KhataEntryTotals,
    KhataEntryUpdate,
)
from app.services import khata_entry as entry_service

# Two routers: entries are created and listed under their khata, but addressed
# directly once they exist. `/khata/entries/{id}` is registered first so it is not
# swallowed by `/khata/{khata_id}`, which would read "entries" as a khata id.
entry_router = APIRouter(prefix="/khata/entries", tags=["khata"])
router = APIRouter(prefix="/khata", tags=["khata"])


def _to_read(entry: KhataEntry, running_balance: Decimal) -> KhataEntryRead:
    return KhataEntryRead(
        id=entry.id,
        khata_id=entry.khata_id,
        entry_type=entry.entry_type,
        amount=entry.amount,
        signed_amount=entry.signed_amount,
        entry_date=entry.entry_date,
        description=entry.description,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        running_balance=running_balance,
    )


@router.get(
    "/{khata_id}/entries",
    response_model=KhataEntryListPage,
    summary="A khata's ledger",
)
def list_entries(
    khata_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    entry_type: Annotated[KhataEntryType | None, Query(description="Only this kind.")] = None,
    start_date: Annotated[date | None, Query(description="Inclusive lower bound.")] = None,
    end_date: Annotated[date | None, Query(description="Inclusive upper bound.")] = None,
    search: Annotated[str | None, Query(max_length=200, description="Match the note.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KhataEntryListPage:
    """Newest first, each entry carrying the balance as it stood after it.

    `running_balance` is computed over the khata's whole history, so it keeps its
    meaning on page two and under a date filter. `balance` and `totals` describe the
    khata itself and are likewise unaffected by the filters on the page — a filtered
    view must not look like the khata is settled when it is not.
    """
    if start_date and end_date and start_date > end_date:
        raise BadRequestError("The start date must not be after the end date.")

    rows, total, balance = entry_service.list_for_khata(
        db,
        khata_id,
        current_user.id,
        entry_type=entry_type,
        start_date=start_date,
        end_date=end_date,
        search=search,
        limit=limit,
        offset=offset,
    )

    from app.services import khata as khata_service

    khata, _, _, _ = khata_service.get_or_404(db, khata_id, current_user.id)
    totals = entry_service.totals_for_khata(db, khata_id)

    return KhataEntryListPage(
        items=[_to_read(entry, running) for entry, running in rows],
        total=total,
        limit=limit,
        offset=offset,
        currency=khata.currency,
        balance=balance,
        totals=KhataEntryTotals(
            given=totals["given"],
            received=totals["received"],
            adjustment=totals["adjustment"],
            balance=balance,
        ),
    )


@router.post(
    "/{khata_id}/entries",
    response_model=KhataEntryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an entry",
)
def create_entry(
    khata_id: uuid.UUID,
    payload: KhataEntryCreate,
    db: DbSession,
    current_user: ActiveUser,
) -> KhataEntryRead:
    """`given` increases what they owe you, `received` reduces it.

    An `adjustment` is a correction and is the only type whose amount may be
    negative. No balance is stored anywhere: every total is summed from these rows.
    """
    entry = entry_service.create(db, khata_id, current_user, payload)

    # The running balance is a property of the entry's position in the whole
    # ledger, so it is read back rather than guessed at from this one row.
    rows, _, _ = entry_service.list_for_khata(db, khata_id, current_user.id, limit=100)
    running = next((balance for item, balance in rows if item.id == entry.id), Decimal("0.00"))
    return _to_read(entry, running)


@entry_router.patch("/{entry_id}", response_model=KhataEntryRead, summary="Edit an entry")
def update_entry(
    entry_id: uuid.UUID,
    payload: KhataEntryUpdate,
    db: DbSession,
    current_user: ActiveUser,
) -> KhataEntryRead:
    """Editing an entry moves the balance, because the balance is only ever the
    sum of these lines."""
    entry, khata = entry_service.get_entry_or_404(db, entry_id, current_user.id)
    entry = entry_service.update(db, entry, khata, payload)

    rows, _, _ = entry_service.list_for_khata(db, khata.id, current_user.id, limit=100)
    running = next((balance for item, balance in rows if item.id == entry.id), Decimal("0.00"))
    return _to_read(entry, running)


@entry_router.delete("/{entry_id}", response_model=Message, summary="Delete an entry")
def delete_entry(entry_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    """Removing a line removes its effect on the balance."""
    entry, khata = entry_service.get_entry_or_404(db, entry_id, current_user.id)
    entry_service.delete(db, entry, khata)
    return Message(message="Entry deleted.")
