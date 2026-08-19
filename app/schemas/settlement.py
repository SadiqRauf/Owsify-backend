"""Settlement and balance schemas."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.currencies import DEFAULT_CURRENCY, normalise_currency
from app.models.settlement import PaymentMethod
from app.schemas.user import UserRead


class SettlementBase(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)
    settled_on: date
    method: PaymentMethod = PaymentMethod.CASH
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        return normalise_currency(value)

    @field_validator("settled_on")
    @classmethod
    def _not_future(cls, value: date) -> date:
        from datetime import timedelta

        if value > date.today() + timedelta(days=1):
            raise ValueError("A settlement cannot be dated in the future.")
        return value


class SettlementCreate(SettlementBase):
    group_id: uuid.UUID | None = Field(
        default=None, description="Omit to settle outside any group."
    )
    from_user_id: uuid.UUID = Field(description="Who paid.")
    to_user_id: uuid.UUID = Field(description="Who received the money.")


class SettlementUpdate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    settled_on: date | None = None
    method: PaymentMethod | None = None
    notes: str | None = Field(default=None, max_length=2000)


class SettlementRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    group_id: uuid.UUID | None
    from_user: UserRead
    to_user: UserRead
    created_by: UserRead
    amount: Decimal
    currency: str
    settled_on: date
    method: PaymentMethod
    notes: str | None
    created_at: datetime
    updated_at: datetime


class SettlementListPage(BaseModel):
    items: list[SettlementRead]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Balances
# --------------------------------------------------------------------------- #
class DebtRead(BaseModel):
    """A directed amount: `debtor` owes `creditor`."""

    debtor: UserRead
    creditor: UserRead
    amount: Decimal
    currency: str


class PersonBalance(BaseModel):
    """The caller's position against one other person, in one currency."""

    user: UserRead
    currency: str
    amount: Decimal = Field(description="Positive: they owe you. Negative: you owe them.")


class CurrencyTotals(BaseModel):
    currency: str
    owed_to_you: Decimal
    you_owe: Decimal
    net: Decimal


class BalanceOverview(BaseModel):
    """Everything the dashboard needs, split by currency so nothing is summed
    across units that cannot legitimately be added together."""

    totals: list[CurrencyTotals]
    people: list[PersonBalance]


class MemberBalance(BaseModel):
    """One group member's net position inside that group."""

    user: UserRead
    currency: str
    net: Decimal = Field(description="Positive when the group owes them.")


class GroupBalanceOverview(BaseModel):
    group_id: uuid.UUID
    currency: str
    total_expenses: Decimal
    total_settled: Decimal
    your_share: Decimal = Field(description="What the caller's expenses in this group total.")
    your_net: Decimal = Field(description="Positive when the caller is owed.")
    members: list[MemberBalance]
    debts: list[DebtRead] = Field(description="Real pairwise debts, not simplified.")


class SimplifiedPlan(BaseModel):
    """The fewest transfers that would settle everyone up."""

    currency: str
    transfers: list[DebtRead]
    transfer_count: int
    original_count: int = Field(description="How many pairwise debts this replaces.")


# --------------------------------------------------------------------------- #
# Activity
# --------------------------------------------------------------------------- #
class ActivityGroupRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    emoji: str | None = None


class ActivityItemRead(BaseModel):
    id: uuid.UUID
    type: str = Field(examples=["expense", "settlement"])
    occurred_at: datetime
    actor: UserRead
    summary: str = Field(examples=["Sadiq added Dinner"])
    amount: Decimal
    currency: str
    group: ActivityGroupRef | None = None
    counterparty: UserRead | None = None
    your_impact: Decimal = Field(
        description="Positive when this left you owed money, negative when owing."
    )


class ActivityPage(BaseModel):
    items: list[ActivityItemRead]
    total: int
    limit: int
    offset: int
