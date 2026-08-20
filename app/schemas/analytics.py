"""Dashboard and analytics schemas."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.models.expense import ExpenseCategory
from app.schemas.expense import ExpenseRead
from app.schemas.settlement import CurrencyTotals, PersonBalance, SettlementRead


class CategorySpending(BaseModel):
    category: ExpenseCategory
    amount: Decimal
    share_of_total: Decimal = Field(description="Percentage of the window's total, 0-100.")
    expense_count: int


class MonthSpending(BaseModel):
    month: str = Field(examples=["2026-08"], description="ISO year-month.")
    amount: Decimal
    expense_count: int


class GroupRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    emoji: str | None = None
    currency: str


class GroupSpending(BaseModel):
    group: GroupRef
    amount: Decimal


class GroupStatistics(BaseModel):
    group: GroupRef
    total_expenses: Decimal
    your_share: Decimal
    your_net: Decimal = Field(description="Positive when the group owes you.")
    expense_count: int
    member_count: int


class DashboardWindow(BaseModel):
    start: date | None = None
    end: date | None = None
    currency: str


class Dashboard(BaseModel):
    """Everything the dashboard needs in one round trip.

    Monetary figures are all in `window.currency`; nothing is summed across
    currencies. `balances` still lists every currency the user has a position in,
    so a single-currency dashboard never hides money.
    """

    window: DashboardWindow

    total_spent: Decimal = Field(description="Your share of expenses in the window.")
    expense_count: int

    balances: list[CurrencyTotals] = Field(description="Per-currency totals, all currencies.")
    people: list[PersonBalance] = Field(description="Who owes whom, all currencies.")

    by_category: list[CategorySpending]
    by_month: list[MonthSpending]
    by_group: list[GroupSpending]

    groups: list[GroupStatistics]
    recent_expenses: list[ExpenseRead]
    recent_settlements: list[SettlementRead]
