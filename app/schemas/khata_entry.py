"""Khata entry request and response schemas."""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.khata import KhataEntryType


class KhataEntryBase(BaseModel):
    entry_type: KhataEntryType
    amount: Decimal = Field(
        max_digits=12,
        decimal_places=2,
        description=(
            "Positive for given and received — the type carries the direction. "
            "An adjustment may be negative, since a correction has no direction "
            "of its own."
        ),
    )
    entry_date: date
    description: str | None = Field(default=None, max_length=200)

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("entry_date")
    @classmethod
    def _not_far_future(cls, value: date) -> date:
        # A day of slack covers a client in a timezone ahead of the server.
        if value > date.today() + timedelta(days=1):
            raise ValueError("An entry cannot be dated in the future.")
        return value

    @model_validator(mode="after")
    def _amount_matches_type(self) -> "KhataEntryBase":
        if self.amount == 0:
            raise ValueError("Enter an amount.")
        if self.entry_type is not KhataEntryType.ADJUSTMENT and self.amount < 0:
            raise ValueError(
                "Only an adjustment can be negative — use Given or Received to set "
                "the direction and keep the amount positive."
            )
        return self


class KhataEntryCreate(KhataEntryBase):
    pass


class KhataEntryUpdate(BaseModel):
    """All optional — a PATCH."""

    entry_type: KhataEntryType | None = None
    amount: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    entry_date: date | None = None
    description: str | None = Field(default=None, max_length=200)

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("entry_date")
    @classmethod
    def _not_far_future(cls, value: date | None) -> date | None:
        if value is not None and value > date.today() + timedelta(days=1):
            raise ValueError("An entry cannot be dated in the future.")
        return value

    @model_validator(mode="after")
    def _something_to_do(self) -> "KhataEntryUpdate":
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        return self


class KhataEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    khata_id: uuid.UUID
    entry_type: KhataEntryType
    amount: Decimal
    signed_amount: Decimal = Field(
        description="Effect on the balance: positive increases what they owe you."
    )
    entry_date: date
    description: str | None
    created_at: datetime
    updated_at: datetime

    # The balance as it stood after this entry, over the khata's whole history —
    # not just the rows on this page.
    running_balance: Decimal


class KhataEntryTotals(BaseModel):
    """The arithmetic behind the balance, as the ledger footer shows it."""

    given: Decimal
    received: Decimal
    adjustment: Decimal
    balance: Decimal


class KhataEntryListPage(BaseModel):
    items: list[KhataEntryRead]
    total: int
    limit: int
    offset: int

    currency: str
    # The khata's real balance, unaffected by any filter applied to the page.
    balance: Decimal
    totals: KhataEntryTotals
