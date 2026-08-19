"""Expense request and response schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.currencies import DEFAULT_CURRENCY, normalise_currency
from app.models.expense import ExpenseCategory, SplitType
from app.schemas.user import UserRead

# Amounts cross the wire as decimal strings so no rounding happens in JSON.
Money = Decimal


class SplitParticipant(BaseModel):
    """One person in a split. `value` means different things per split type."""

    user_id: uuid.UUID
    value: Decimal | None = Field(
        default=None,
        description=(
            "Ignored for an equal split, the exact share for an exact split, "
            "and the percentage for a percentage split."
        ),
    )

    @field_validator("value")
    @classmethod
    def _not_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("Split values cannot be negative.")
        return value


class ExpenseBase(BaseModel):
    description: str = Field(min_length=1, max_length=200)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)
    expense_date: date
    category: ExpenseCategory = ExpenseCategory.GENERAL
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Description cannot be blank.")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        return normalise_currency(value)

    @field_validator("expense_date")
    @classmethod
    def _not_absurdly_future(cls, value: date) -> date:
        # A day of slack covers clients in a timezone ahead of the server.
        from datetime import timedelta

        if value > date.today() + timedelta(days=1):
            raise ValueError("An expense cannot be dated in the future.")
        return value


class ExpenseCreate(ExpenseBase):
    group_id: uuid.UUID | None = Field(
        default=None, description="Omit for a one-to-one expense between friends."
    )
    paid_by_id: uuid.UUID
    split_type: SplitType = SplitType.EQUAL
    splits: list[SplitParticipant] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _values_match_split_type(self) -> "ExpenseCreate":
        if self.split_type is SplitType.EQUAL:
            return self
        missing = [split.user_id for split in self.splits if split.value is None]
        if missing:
            label = "an amount" if self.split_type is SplitType.EXACT else "a percentage"
            raise ValueError(f"Every participant needs {label} for a {self.split_type} split.")
        return self


class ExpenseUpdate(BaseModel):
    """All optional — a PATCH. Supplying `splits` requires `split_type` too."""

    description: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    expense_date: date | None = None
    category: ExpenseCategory | None = None
    notes: str | None = Field(default=None, max_length=2000)
    paid_by_id: uuid.UUID | None = None
    split_type: SplitType | None = None
    splits: list[SplitParticipant] | None = Field(default=None, max_length=100)

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Description cannot be blank.")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str | None) -> str | None:
        return normalise_currency(value) if value else value

    @model_validator(mode="after")
    def _splits_come_with_a_type(self) -> "ExpenseUpdate":
        if self.splits is not None and self.split_type is None:
            raise ValueError("Send split_type alongside splits so they can be interpreted.")
        if self.splits is not None and not self.splits:
            raise ValueError("An expense needs at least one participant.")
        return self


class ExpenseSplitRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user: UserRead
    amount: Decimal
    percentage: Decimal | None = None


class ExpenseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    group_id: uuid.UUID | None
    description: str
    amount: Decimal
    currency: str
    expense_date: date
    category: ExpenseCategory
    split_type: SplitType
    notes: str | None
    paid_by: UserRead
    created_by: UserRead
    created_at: datetime
    updated_at: datetime
    splits: list[ExpenseSplitRead]

    # Filled in per request: what this expense does to the caller's balance.
    my_share: Decimal = Field(default=Decimal("0.00"), description="What the caller owes on it.")
    my_net: Decimal = Field(
        default=Decimal("0.00"),
        description="Positive when the caller is owed money, negative when they owe.",
    )


class ExpenseListPage(BaseModel):
    items: list[ExpenseRead]
    total: int
    limit: int
    offset: int


class BalanceEntry(BaseModel):
    """Net position between the caller and one other person."""

    user: UserRead
    amount: Decimal = Field(description="Positive: they owe you. Negative: you owe them.")
    currency: str


class BalanceSummary(BaseModel):
    currency: str
    total_owed_to_you: Decimal
    total_you_owe: Decimal
    net: Decimal
    entries: list[BalanceEntry]
