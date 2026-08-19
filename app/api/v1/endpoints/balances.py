"""Balance views: what you are owed, what you owe, and the shortest way to settle."""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ActiveUser, DbSession
from app.models.user import User
from app.schemas.settlement import (
    BalanceOverview,
    CurrencyTotals,
    DebtRead,
    GroupBalanceOverview,
    MemberBalance,
    PersonBalance,
    SimplifiedPlan,
)
from app.schemas.user import UserRead
from app.services import balance as balance_service
from app.services import group as group_service
from app.services import user as user_service
from app.services.balance import Debt, from_cents

router = APIRouter(prefix="/balances", tags=["balances"])

ZERO = Decimal("0.00")


def _users_by_id(db: DbSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    """One lookup for every person a balance view mentions."""
    if not ids:
        return {}
    from sqlalchemy import select

    return {user.id: user for user in db.scalars(select(User).where(User.id.in_(ids)))}


def _to_debt_read(debt: Debt, people: dict[uuid.UUID, User]) -> DebtRead | None:
    debtor = people.get(debt.debtor_id)
    creditor = people.get(debt.creditor_id)
    if debtor is None or creditor is None:
        return None
    return DebtRead(
        debtor=UserRead.model_validate(debtor),
        creditor=UserRead.model_validate(creditor),
        amount=debt.amount,
        currency=debt.currency,
    )


@router.get("/me", response_model=BalanceOverview, summary="Your balances everywhere")
def read_my_balances(db: DbSession, current_user: ActiveUser) -> BalanceOverview:
    """Totals and per-person positions, kept separate per currency.

    Amounts in different currencies are never added together — a net across USD and
    EUR would be a number with no meaning.
    """
    ledger = balance_service.build_ledger(db, viewer_id=current_user.id)

    totals = [
        CurrencyTotals(
            currency=summary.currency,
            owed_to_you=summary.owed_to_you,
            you_owe=summary.you_owe,
            net=summary.net,
        )
        for summary in balance_service.summarise_for_user(ledger, current_user.id)
    ]

    wanted: set[uuid.UUID] = set()
    positions: list[tuple[str, uuid.UUID, int]] = []
    for currency in ledger.currencies:
        for other_id, cents in ledger.counterparties(current_user.id, currency).items():
            positions.append((currency, other_id, cents))
            wanted.add(other_id)

    people = _users_by_id(db, wanted)

    entries = [
        PersonBalance(
            user=UserRead.model_validate(people[other_id]),
            currency=currency,
            amount=from_cents(cents),
        )
        for currency, other_id, cents in positions
        if other_id in people
    ]
    entries.sort(key=lambda entry: (entry.currency, -entry.amount))

    return BalanceOverview(totals=totals, people=entries)


@router.get(
    "/groups/{group_id}",
    response_model=GroupBalanceOverview,
    summary="Balances inside one group",
)
def read_group_balances(
    group_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> GroupBalanceOverview:
    """Per-member net positions plus the real pairwise debts behind them."""
    from sqlalchemy import func, select

    from app.models.expense import Expense
    from app.models.settlement import Settlement

    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)

    ledger = balance_service.build_ledger(db, group_id=group_id)
    currency = group.currency

    total_expenses = (
        db.scalar(
            select(func.coalesce(func.sum(Expense.amount), 0)).where(Expense.group_id == group_id)
        )
        or ZERO
    )
    total_settled = (
        db.scalar(
            select(func.coalesce(func.sum(Settlement.amount), 0)).where(
                Settlement.group_id == group_id
            )
        )
        or ZERO
    )
    your_share = (
        db.scalar(
            select(func.coalesce(func.sum(Expense.amount), 0))
            .where(Expense.group_id == group_id, Expense.paid_by_id == current_user.id)
        )
        or ZERO
    )

    members = [
        MemberBalance(
            user=UserRead.model_validate(member.user),
            currency=currency,
            net=from_cents(ledger.net_for(member.user_id, currency)),
        )
        for member in group.members
    ]
    members.sort(key=lambda entry: -entry.net)

    people = {member.user_id: member.user for member in group.members}
    debts = [
        read
        for debt in balance_service.who_owes_whom(ledger, currency)
        if (read := _to_debt_read(debt, people)) is not None
    ]

    return GroupBalanceOverview(
        group_id=group_id,
        currency=currency,
        total_expenses=Decimal(total_expenses).quantize(Decimal("0.01")),
        total_settled=Decimal(total_settled).quantize(Decimal("0.01")),
        your_share=Decimal(your_share).quantize(Decimal("0.01")),
        your_net=from_cents(ledger.net_for(current_user.id, currency)),
        members=members,
        debts=debts,
    )


@router.get(
    "/groups/{group_id}/simplified",
    response_model=SimplifiedPlan,
    summary="Fewest transfers to settle a group",
)
def read_simplified_group_plan(
    group_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> SimplifiedPlan:
    """Collapses a web of debts into the minimum number of payments.

    This deliberately reassigns who pays whom — someone may end up paying a person
    they never borrowed from directly. It is offered alongside the real pairwise
    debts rather than replacing them, so nobody is surprised by a payment request
    they cannot trace.
    """
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)

    ledger = balance_service.build_ledger(db, group_id=group_id)
    currency = group.currency

    people = {member.user_id: member.user for member in group.members}
    transfers = [
        read
        for debt in balance_service.simplify(ledger, currency)
        if (read := _to_debt_read(debt, people)) is not None
    ]
    original = balance_service.who_owes_whom(ledger, currency)

    return SimplifiedPlan(
        currency=currency,
        transfers=transfers,
        transfer_count=len(transfers),
        original_count=len(original),
    )


@router.get(
    "/simplified",
    response_model=list[SimplifiedPlan],
    summary="Fewest transfers across everything you are part of",
)
def read_simplified_overall(db: DbSession, current_user: ActiveUser) -> list[SimplifiedPlan]:
    ledger = balance_service.build_ledger(db, viewer_id=current_user.id)

    wanted: set[uuid.UUID] = set()
    for currency in ledger.currencies:
        wanted |= ledger.participants(currency)
    people = _users_by_id(db, wanted)

    plans: list[SimplifiedPlan] = []
    for currency in ledger.currencies:
        transfers = [
            read
            for debt in balance_service.simplify(ledger, currency)
            if (read := _to_debt_read(debt, people)) is not None
        ]
        plans.append(
            SimplifiedPlan(
                currency=currency,
                transfers=transfers,
                transfer_count=len(transfers),
                original_count=len(balance_service.who_owes_whom(ledger, currency)),
            )
        )
    return plans


@router.get(
    "/with/{user_id}",
    response_model=list[PersonBalance],
    summary="Your balance with one person",
)
def read_balance_with(
    user_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    group_id: Annotated[uuid.UUID | None, Query(description="Restrict to one group.")] = None,
) -> list[PersonBalance]:
    """One entry per currency in which the two of you have a non-zero position."""
    other = user_service.get_or_404(db, user_id)

    if group_id is not None:
        group = group_service.get_or_404(db, group_id)
        group_service.require_membership(group, current_user.id)

    ledger = balance_service.build_ledger(db, viewer_id=current_user.id, group_id=group_id)

    return [
        PersonBalance(
            user=UserRead.model_validate(other),
            currency=currency,
            amount=from_cents(ledger.between(current_user.id, user_id, currency)),
        )
        for currency in ledger.currencies
        if ledger.between(current_user.id, user_id, currency) != 0
    ]
