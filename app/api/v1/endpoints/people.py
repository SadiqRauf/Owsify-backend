"""People: one page per person, adding up every way you owe each other."""

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ActiveUser, DbSession
from app.core.currencies import normalise_currency
from app.schemas.person import (
    PersonActivityPage,
    PersonActivityRead,
    PersonBalanceBreakdown,
    PersonGroupRef,
    PersonListItem,
    PersonListPage,
    PersonSummaryRead,
)
from app.schemas.user import UserRead
from app.services import balance as balance_service
from app.services import khata_entry as entry_service
from app.services import person as person_service

router = APIRouter(prefix="/people", tags=["people"])


@router.get("", response_model=PersonListPage, summary="Everyone you owe or are owed by")
def list_people(
    db: DbSession,
    current_user: ActiveUser,
    search: Annotated[str | None, Query(max_length=200)] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
) -> PersonListPage:
    """People you share money with, plus khata contacts who have no account.

    The second group is marked with `has_account: false` and carries a `khata_id`
    instead of a user — they have no user id, so no person page can exist for them.
    """
    code = normalise_currency(currency) if currency else current_user.currency
    # One ledger, reused for every row: building it per person would re-read the
    # whole expense history once per contact.
    ledger = balance_service.build_ledger(db, viewer_id=current_user.id)

    items = [
        PersonListItem(
            id=person.id,
            name=person.full_name,
            email=person.email,
            avatar_url=person.avatar_url,
            user=UserRead.model_validate(person),
            currency=code,
            total_balance=person_service.summarise(
                db, current_user, person.id, currency=code, ledger=ledger, with_counts=False
            ).total_balance,
            has_account=True,
        )
        for person in person_service.list_people(db, current_user, search=search)
    ]

    for khata in person_service.unlinked_khatas(db, current_user, search=search):
        totals = entry_service.totals_for_khata(db, khata.id)
        items.append(
            PersonListItem(
                id=khata.id,
                name=khata.person_name,
                email=khata.person_email,
                khata_id=khata.id,
                currency=khata.currency,
                total_balance=(
                    totals["given"] - totals["received"] + totals["adjustment"]
                ),
                has_account=False,
            )
        )

    items.sort(key=lambda item: (-abs(item.total_balance), item.name.lower()))
    return PersonListPage(items=items, total=len(items))


@router.get(
    "/{user_id}/summary",
    response_model=PersonSummaryRead,
    summary="Everything you owe each other, in one number",
)
def read_summary(
    user_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
) -> PersonSummaryRead:
    """Group expenses, khata and loans, added up.

    Every component is signed the same way — positive means they owe you — so the
    total is a plain sum. `loan_balance` is always 0.00: the app has no loans
    feature yet, and the field is reported rather than dropped so the breakdown is
    visibly complete rather than quietly partial.

    The group figure comes from the balance engine that the group pages use, so this
    page can never disagree with them.
    """
    code = normalise_currency(currency) if currency else current_user.currency
    summary = person_service.summarise(db, current_user, user_id, currency=code)
    groups = person_service.shared_groups(db, current_user.id, user_id)
    recent, _ = person_service.person_activity(db, current_user, user_id, limit=10)

    return PersonSummaryRead(
        person=UserRead.model_validate(summary.person),
        currency=summary.currency,
        balances=PersonBalanceBreakdown(
            group_balance=summary.group_balance,
            khata_balance=summary.khata_balance,
            loan_balance=summary.loan_balance,
            total_balance=summary.total_balance,
            settled_total=summary.settled_total,
        ),
        shared_group_count=summary.shared_group_count,
        khata_count=summary.khata_count,
        expense_count=summary.expense_count,
        khata_ids=summary.khata_ids,
        shared_groups=[PersonGroupRef.model_validate(group) for group in groups],
        recent_activity=[PersonActivityRead(**asdict(item)) for item in recent],
    )


@router.get(
    "/{user_id}/activity",
    response_model=PersonActivityPage,
    summary="Everything that has happened between you",
)
def read_activity(
    user_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> PersonActivityPage:
    """Expenses, settlements and khata entries, merged newest-first."""
    items, total = person_service.person_activity(
        db, current_user, user_id, limit=limit, offset=offset
    )
    return PersonActivityPage(
        items=[PersonActivityRead(**asdict(item)) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )
