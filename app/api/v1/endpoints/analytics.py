"""Dashboard and analytics endpoints."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ActiveUser, DbSession
from app.api.v1.endpoints.expenses import to_expense_read
from app.core.currencies import normalise_currency
from app.core.exceptions import BadRequestError
from app.schemas.analytics import (
    CategorySpending,
    Dashboard,
    DashboardWindow,
    GroupRef,
    GroupSpending,
    GroupStatistics,
    MonthSpending,
)
from app.schemas.settlement import CurrencyTotals, PersonBalance, SettlementRead
from app.schemas.user import UserRead
from app.services import analytics as analytics_service
from app.services import balance as balance_service
from app.services import group as group_service
from app.services import user as user_service
from app.services.analytics import DateRange

router = APIRouter(prefix="/analytics", tags=["analytics"])

StartDate = Annotated[date | None, Query(description="Inclusive lower bound on expense date.")]
EndDate = Annotated[date | None, Query(description="Inclusive upper bound on expense date.")]
CurrencyParam = Annotated[
    str | None,
    Query(min_length=3, max_length=3, description="Defaults to your own currency."),
]


def _window(start: date | None, end: date | None) -> DateRange:
    if start and end and start > end:
        raise BadRequestError("The start date must not be after the end date.")
    return DateRange(start=start, end=end)


def _currency(requested: str | None, fallback: str) -> str:
    if requested is None:
        return fallback
    try:
        return normalise_currency(requested)
    except ValueError as error:
        raise BadRequestError(str(error)) from error


@router.get("/categories", response_model=list[CategorySpending], summary="Spending by category")
def read_category_spending(
    db: DbSession,
    current_user: ActiveUser,
    start_date: StartDate = None,
    end_date: EndDate = None,
    currency: CurrencyParam = None,
    group_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[CategorySpending]:
    """Your **share** of expenses per category — what you consumed, not what you paid."""
    if group_id is not None:
        group = group_service.get_or_404(db, group_id)
        group_service.require_membership(group, current_user.id)

    rows = analytics_service.spending_by_category(
        db,
        current_user.id,
        _currency(currency, current_user.currency),
        _window(start_date, end_date),
        group_id=group_id,
    )
    return [
        CategorySpending(
            category=row.category,
            amount=row.amount,
            share_of_total=row.share_of_total,
            expense_count=row.expense_count,
        )
        for row in rows
    ]


@router.get("/monthly", response_model=list[MonthSpending], summary="Spending by month")
def read_monthly_spending(
    db: DbSession,
    current_user: ActiveUser,
    months: Annotated[int, Query(ge=1, le=24)] = 6,
    currency: CurrencyParam = None,
    group_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[MonthSpending]:
    """Oldest first. Quiet months come back as zero so the time axis stays even."""
    if group_id is not None:
        group = group_service.get_or_404(db, group_id)
        group_service.require_membership(group, current_user.id)

    rows = analytics_service.monthly_spending(
        db,
        current_user.id,
        _currency(currency, current_user.currency),
        months=months,
        group_id=group_id,
    )
    return [
        MonthSpending(month=row.month, amount=row.amount, expense_count=row.expense_count)
        for row in rows
    ]


@router.get("/groups", response_model=list[GroupStatistics], summary="Per-group statistics")
def read_group_statistics(
    db: DbSession,
    current_user: ActiveUser,
    start_date: StartDate = None,
    end_date: EndDate = None,
) -> list[GroupStatistics]:
    rows = analytics_service.group_statistics(db, current_user.id, _window(start_date, end_date))
    return [
        GroupStatistics(
            group=GroupRef.model_validate(row.group),
            total_expenses=row.total_expenses,
            your_share=row.your_share,
            your_net=row.your_net,
            expense_count=row.expense_count,
            member_count=row.member_count,
        )
        for row in rows
    ]


@router.get("/dashboard", response_model=Dashboard, summary="Everything the dashboard needs")
def read_dashboard(
    db: DbSession,
    current_user: ActiveUser,
    start_date: StartDate = None,
    end_date: EndDate = None,
    currency: CurrencyParam = None,
    months: Annotated[int, Query(ge=1, le=24)] = 6,
) -> Dashboard:
    """One round trip for the whole dashboard.

    Spending figures are scoped to a single currency; balances are not, so a user
    holding positions in more than one still sees all of them.
    """
    window = _window(start_date, end_date)
    code = _currency(currency, current_user.currency)

    total_spent, expense_count = analytics_service.totals_for_window(
        db, current_user.id, code, window
    )

    ledger = balance_service.build_ledger(db, viewer_id=current_user.id)
    balances = [
        CurrencyTotals(
            currency=summary.currency,
            owed_to_you=summary.owed_to_you,
            you_owe=summary.you_owe,
            net=summary.net,
        )
        for summary in balance_service.summarise_for_user(ledger, current_user.id)
    ]

    people: list[PersonBalance] = []
    for code_in_ledger in ledger.currencies:
        for other_id, cents in ledger.counterparties(current_user.id, code_in_ledger).items():
            other = user_service.get_by_id(db, other_id)
            if other is None:
                continue
            people.append(
                PersonBalance(
                    user=UserRead.model_validate(other),
                    currency=code_in_ledger,
                    amount=balance_service.from_cents(cents),
                )
            )
    people.sort(key=lambda entry: (entry.currency, -entry.amount))

    return Dashboard(
        window=DashboardWindow(start=window.start, end=window.end, currency=code),
        total_spent=total_spent,
        expense_count=expense_count,
        balances=balances,
        people=people,
        by_category=[
            CategorySpending(
                category=row.category,
                amount=row.amount,
                share_of_total=row.share_of_total,
                expense_count=row.expense_count,
            )
            for row in analytics_service.spending_by_category(db, current_user.id, code, window)
        ],
        by_month=[
            MonthSpending(month=row.month, amount=row.amount, expense_count=row.expense_count)
            for row in analytics_service.monthly_spending(
                db, current_user.id, code, months=months
            )
        ],
        by_group=[
            GroupSpending(group=GroupRef.model_validate(group), amount=amount)
            for group, amount in analytics_service.spending_by_group(
                db, current_user.id, code, window
            )
        ],
        groups=[
            GroupStatistics(
                group=GroupRef.model_validate(row.group),
                total_expenses=row.total_expenses,
                your_share=row.your_share,
                your_net=row.your_net,
                expense_count=row.expense_count,
                member_count=row.member_count,
            )
            for row in analytics_service.group_statistics(db, current_user.id, window)
        ],
        recent_expenses=[
            to_expense_read(expense, current_user.id)
            for expense in analytics_service.recent_expenses(db, current_user.id)
        ],
        recent_settlements=[
            SettlementRead.model_validate(settlement)
            for settlement in analytics_service.recent_settlements(db, current_user.id)
        ],
    )
