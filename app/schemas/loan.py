"""Loan and payment payloads.

`status`, `paid` and `remaining` are read-only on the way out and derived from the
payments — see `app/models/loan.py` for why none of them is a stored column.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.currencies import DEFAULT_CURRENCY, normalise_currency
from app.models.loan import LoanDirection, LoanStatus
from app.schemas.user import UserRead

DEFAULT_LOAN_CURRENCY = "PKR"

# Loans are commonly agreed with a date well in the future, and just as commonly
# recorded after the fact. Both directions are allowed; only absurd values are not.
MAX_YEARS_AHEAD = 50


class LoanBase(BaseModel):
    direction: LoanDirection = Field(
        default=LoanDirection.GIVEN,
        description=(
            "`given` — you lent it and they owe you. `taken` — you borrowed it and "
            "you owe them."
        ),
    )
    counterparty_name: str = Field(
        min_length=1, max_length=120, examples=["Ahmed"],
        description="The other person, whichever way the money went.",
    )
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2, examples=["50000.00"])
    currency: str = Field(default=DEFAULT_LOAN_CURRENCY, min_length=3, max_length=3)
    due_date: date | None = Field(
        default=None,
        description="Optional. Plenty of real lending has no agreed date.",
        examples=["2026-09-20"],
    )
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("counterparty_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Who is this loan with?")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        return normalise_currency(value)

    @field_validator("due_date")
    @classmethod
    def _sane_due_date(cls, value: date | None) -> date | None:
        if value is not None and value.year > date.today().year + MAX_YEARS_AHEAD:
            raise ValueError("That due date is too far in the future.")
        return value


class LoanCreate(LoanBase):
    counterparty_user_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Set when the other person has an account. Optional on purpose: lending "
            "to someone who will never install the app is the ordinary case."
        ),
    )


class LoanUpdate(BaseModel):
    """Every field optional; at least one required."""

    direction: LoanDirection | None = None
    counterparty_name: str | None = Field(default=None, min_length=1, max_length=120)
    counterparty_user_id: uuid.UUID | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    due_date: date | None = None
    description: str | None = Field(default=None, max_length=2000)
    status: LoanStatus | None = Field(
        default=None,
        description=(
            "Only CANCELLED is meaningful here — it is the one status that is a "
            "decision rather than a consequence of the payments. Sending any other "
            "value on a cancelled loan reopens it. To mark a loan paid, use "
            "POST /loans/{id}/settle, which records the payment that closes it."
        ),
    )

    @field_validator("counterparty_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Who is this loan with?")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str | None) -> str | None:
        return normalise_currency(value) if value else None

    @model_validator(mode="after")
    def _something_to_do(self) -> "LoanUpdate":
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        return self


class LoanPaymentCreate(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2, examples=["20000.00"])
    payment_date: date = Field(examples=["2026-08-21"])
    note: str | None = Field(default=None, max_length=200)

    @field_validator("payment_date")
    @classmethod
    def _not_in_the_future(cls, value: date) -> date:
        # One day of slack, so a client in a timezone ahead of the server is not
        # told that today is in the future.
        if value > date.today() + timedelta(days=1):
            raise ValueError("A payment cannot be dated in the future.")
        return value


class LoanPaymentUpdate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    payment_date: date | None = None
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _something_to_do(self) -> "LoanPaymentUpdate":
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        return self


class LoanPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    loan_id: uuid.UUID
    amount: Decimal
    payment_date: date
    note: str | None
    created_at: datetime
    updated_at: datetime


class LoanPaymentWithProgress(LoanPaymentRead):
    remaining_after: Decimal = Field(
        description="What was still outstanding once this payment landed."
    )


class LoanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: LoanDirection
    counterparty_name: str
    display_name: str = Field(description="The linked account's name if there is one.")
    counterparty_user: UserRead | None
    amount: Decimal
    currency: str
    due_date: date | None
    description: str | None
    created_at: datetime
    updated_at: datetime
    cancelled_at: datetime | None

    # All of these are derived from the payments, never stored.
    paid: Decimal
    remaining: Decimal = Field(
        description="Still owed on the original direction. Never below zero."
    )
    overpaid: Decimal = Field(
        description=(
            "How much more than the principal came back — money now owed the other "
            "way. Repay 1,500 against a 1,000 loan and this is 500."
        )
    )
    signed_balance: Decimal = Field(
        description=(
            "Where the loan leaves you both, positive means they owe you. Same "
            "convention as every other balance in the app, so this can be added to "
            "khata and group figures without a special case."
        )
    )
    status: LoanStatus
    payment_count: int
    days_until_due: int | None = Field(
        default=None,
        description=(
            "Negative when overdue, null when the loan has no due date or is closed."
        ),
    )


class LoanDetail(LoanRead):
    payments: list[LoanPaymentRead]


class LoanCurrencyTotal(BaseModel):
    """Per currency, and split by direction rather than netted.

    "You are owed 50,000 and you owe 30,000" is a different situation from "you are
    owed 20,000", and one net figure cannot tell them apart — so both sides are
    reported, with `net` offered for anyone who wants the single number.
    """

    currency: str
    lent: Decimal
    borrowed: Decimal
    repaid_to_you: Decimal
    repaid_by_you: Decimal
    receivable: Decimal = Field(description="Total they owe you across all loans.")
    payable: Decimal = Field(description="Total you owe them, overpayments included.")

    given_balance: Decimal = Field(
        description=(
            "Where the loans you gave stand, positive means they owe you. Scoped by "
            "direction rather than by sign, so an overpaid loan you gave stays "
            "counted here — as a negative — instead of moving under the loans you "
            "took."
        )
    )
    taken_balance: Decimal = Field(
        description="Where the loans you took stand. Negative means you owe them."
    )

    net: Decimal = Field(description="given_balance + taken_balance.")
    overdue: Decimal
    loan_count: int
    overdue_count: int


class LoanListPage(BaseModel):
    items: list[LoanRead]
    total: int
    limit: int
    offset: int
    totals: list[LoanCurrencyTotal] = Field(
        description="Per currency: rupees and dollars are never added together."
    )


class LoanPaymentListPage(BaseModel):
    items: list[LoanPaymentWithProgress]
    total: int
    limit: int
    offset: int
    currency: str
    direction: LoanDirection
    loan_amount: Decimal
    paid: Decimal
    remaining: Decimal
    overpaid: Decimal
