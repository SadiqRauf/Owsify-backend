"""Expense CRUD, history, and derived balances."""

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.models.expense import Expense
from app.schemas.common import Message
from app.schemas.expense import (
    BalanceEntry,
    BalanceSummary,
    ExpenseCreate,
    ExpenseListPage,
    ExpenseRead,
    ExpenseSplitRead,
    ExpenseUpdate,
)
from app.schemas.user import UserRead
from app.services import expense as expense_service
from app.services import group as group_service
from app.services import user as user_service

router = APIRouter(prefix="/expenses", tags=["expenses"])

ZERO = Decimal("0.00")


def to_expense_read(expense: Expense, viewer_id: uuid.UUID) -> ExpenseRead:
    """Serialise an expense, annotated with what it means for the caller."""
    return ExpenseRead(
        id=expense.id,
        group_id=expense.group_id,
        description=expense.description,
        amount=expense.amount,
        currency=expense.currency,
        expense_date=expense.expense_date,
        category=expense.category,
        split_type=expense.split_type,
        notes=expense.notes,
        paid_by=UserRead.model_validate(expense.paid_by),
        created_by=UserRead.model_validate(expense.created_by),
        created_at=expense.created_at,
        updated_at=expense.updated_at,
        splits=[
            ExpenseSplitRead(
                user=UserRead.model_validate(split.user),
                amount=split.amount,
                percentage=split.percentage,
            )
            for split in expense.splits
        ],
        my_share=expense.share_for(viewer_id),
        my_net=expense_service.net_for(expense, viewer_id),
    )


@router.get("", response_model=ExpenseListPage, summary="Expenses you can see")
def list_expenses(
    db: DbSession,
    current_user: ActiveUser,
    group_id: Annotated[uuid.UUID | None, Query(description="Restrict to one group.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExpenseListPage:
    """Newest first. Covers group expenses you are a member of plus personal ones."""
    if group_id is not None:
        group = group_service.get_or_404(db, group_id)
        group_service.require_membership(group, current_user.id)

    items, total = expense_service.list_for_user(
        db, current_user.id, group_id=group_id, limit=limit, offset=offset
    )
    return ExpenseListPage(
        items=[to_expense_read(expense, current_user.id) for expense in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ExpenseRead,
    status_code=status.HTTP_201_CREATED,
    summary="Add an expense",
)
def create_expense(
    payload: ExpenseCreate, db: DbSession, current_user: ActiveUser
) -> ExpenseRead:
    """Splits are computed server-side so the shares always add up to the amount.

    `split_type` decides how each participant's `value` is read: ignored for an
    equal split, an exact share for `exact`, a percentage for `percentage`.
    """
    expense = expense_service.create(db, current_user, payload)
    return to_expense_read(expense, current_user.id)


@router.get("/balances", response_model=BalanceSummary, summary="Who owes whom")
def read_balances(
    db: DbSession,
    current_user: ActiveUser,
    group_id: Annotated[uuid.UUID | None, Query(description="Restrict to one group.")] = None,
) -> BalanceSummary:
    """Derived from expense splits alone; settlements arrive in a later milestone."""
    if group_id is not None:
        group = group_service.get_or_404(db, group_id)
        group_service.require_membership(group, current_user.id)

    balances = expense_service.balances_for_user(db, current_user.id, group_id=group_id)

    entries: list[BalanceEntry] = []
    owed_to_you = ZERO
    you_owe = ZERO

    for other_id, amount in balances.items():
        other = user_service.get_by_id(db, other_id)
        if other is None:
            continue
        entries.append(
            BalanceEntry(
                user=UserRead.model_validate(other),
                amount=amount,
                currency=current_user.currency,
            )
        )
        if amount > 0:
            owed_to_you += amount
        else:
            you_owe += -amount

    entries.sort(key=lambda entry: entry.amount, reverse=True)

    return BalanceSummary(
        currency=current_user.currency,
        total_owed_to_you=owed_to_you,
        total_you_owe=you_owe,
        net=owed_to_you - you_owe,
        entries=entries,
    )


@router.get("/{expense_id}", response_model=ExpenseRead, summary="Expense detail")
def read_expense(expense_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> ExpenseRead:
    expense = expense_service.get_or_404(db, expense_id, current_user.id)
    return to_expense_read(expense, current_user.id)


@router.patch("/{expense_id}", response_model=ExpenseRead, summary="Edit an expense")
def update_expense(
    expense_id: uuid.UUID, payload: ExpenseUpdate, db: DbSession, current_user: ActiveUser
) -> ExpenseRead:
    """Send `splits` together with `split_type` to change how it divides."""
    expense = expense_service.get_or_404(db, expense_id, current_user.id)
    expense = expense_service.update(db, expense, current_user, payload)
    return to_expense_read(expense, current_user.id)


@router.delete("/{expense_id}", response_model=Message, summary="Delete an expense")
def delete_expense(expense_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    expense = expense_service.get_or_404(db, expense_id, current_user.id)
    expense_service.delete(db, expense, current_user)
    return Message(message="Expense deleted.")
