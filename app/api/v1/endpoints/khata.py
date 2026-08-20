"""Khata accounts — a running ledger with one other person."""

import uuid
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.models.khata import KhataAccount
from app.schemas.common import Message
from app.schemas.khata import (
    KhataCreate,
    KhataCurrencyTotal,
    KhataListPage,
    KhataRead,
    KhataUpdate,
)
from app.schemas.user import UserRead
from app.services import khata as khata_service

router = APIRouter(prefix="/khata", tags=["khata"])


def _to_read(
    khata: KhataAccount,
    balance: Decimal,
    entry_count: int,
    last_entry_on: object = None,
) -> KhataRead:
    return KhataRead(
        id=khata.id,
        person_name=khata.person_name,
        display_name=khata.display_name,
        person_phone=khata.person_phone,
        person_email=khata.person_email,
        person_user=UserRead.model_validate(khata.person_user) if khata.person_user else None,
        currency=khata.currency,
        notes=khata.notes,
        is_archived=khata.is_archived,
        created_at=khata.created_at,
        updated_at=khata.updated_at,
        balance=balance,
        entry_count=entry_count,
        last_entry_on=last_entry_on,
    )


@router.get("", response_model=KhataListPage, summary="Your khatas")
def list_khatas(
    db: DbSession,
    current_user: ActiveUser,
    search: Annotated[
        str | None, Query(max_length=120, description="Match on name, phone or email.")
    ] = None,
    include_archived: Annotated[bool, Query()] = False,
    sort: Annotated[
        Literal["-updated_at", "updated_at", "person_name", "-person_name", "-created_at", "created_at"],
        Query(description="Prefix with - for descending."),
    ] = "-updated_at",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KhataListPage:
    """Only your own khatas: a khata is a private book, not a shared one.

    Archived khatas are hidden unless asked for. Totals are reported per currency,
    since adding rupees to dollars would produce a meaningless figure.
    """
    rows, total = khata_service.list_for_owner(
        db,
        current_user.id,
        search=search,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
        sort=sort,
    )

    return KhataListPage(
        items=[
            _to_read(row[0], Decimal(row.balance).quantize(Decimal("0.01")), row.entry_count, row.last_entry_on)
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
        totals=[
            KhataCurrencyTotal(**bucket)
            for bucket in khata_service.currency_totals(
                db, current_user.id, include_archived=include_archived
            )
        ],
    )


@router.post(
    "", response_model=KhataRead, status_code=status.HTTP_201_CREATED, summary="Open a khata"
)
def create_khata(payload: KhataCreate, db: DbSession, current_user: ActiveUser) -> KhataRead:
    """`person_name` is all that is required.

    Linking `person_user_id` is optional on purpose: the main use of a khata is
    keeping a book for someone who is not on the app at all. When it is supplied,
    only one khata per person is allowed, so a balance cannot be split across two
    books for the same account.
    """
    khata = khata_service.create(db, current_user, payload)
    return _to_read(khata, Decimal("0.00"), 0, None)


@router.get("/{khata_id}", response_model=KhataRead, summary="One khata")
def read_khata(khata_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> KhataRead:
    khata, balance, entries, last = khata_service.get_or_404(db, khata_id, current_user.id)
    return _to_read(khata, balance, entries, last)


@router.patch("/{khata_id}", response_model=KhataRead, summary="Update or archive a khata")
def update_khata(
    khata_id: uuid.UUID, payload: KhataUpdate, db: DbSession, current_user: ActiveUser
) -> KhataRead:
    """Send `is_archived` to archive or restore; everything else edits the details."""
    khata, _, entries, last = khata_service.get_or_404(db, khata_id, current_user.id)
    khata = khata_service.update(db, khata, current_user, payload)

    _, balance, entries, last = khata_service.get_or_404(db, khata_id, current_user.id)
    return _to_read(khata, balance, entries, last)


@router.delete("/{khata_id}", response_model=Message, summary="Archive or delete a khata")
def delete_khata(
    khata_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    permanent: Annotated[
        bool, Query(description="Delete the khata and every entry in it, irreversibly.")
    ] = False,
) -> Message:
    """Archives by default; `?permanent=true` destroys the ledger.

    A khata is a financial record, so the forgiving action is the default one: a
    single unqualified DELETE hides it and keeps the history. Destroying entries
    has to be asked for explicitly.
    """
    khata, _, entries, _ = khata_service.get_or_404(db, khata_id, current_user.id)

    if permanent:
        khata_service.delete(db, khata)
        return Message(
            message=(
                f"Khata deleted along with {entries} entr{'y' if entries == 1 else 'ies'}."
                if entries
                else "Khata deleted."
            )
        )

    khata_service.archive(db, khata, archived=True)
    return Message(message="Khata archived. Its entries are kept.")
