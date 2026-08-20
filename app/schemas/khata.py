"""Khata request and response schemas."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.currencies import normalise_currency
from app.schemas.user import UserRead

# Khatas are a South Asian ledger convention, so the rupee is the sensible default
# here even though the rest of the app defaults to the user's own currency.
DEFAULT_KHATA_CURRENCY = "PKR"


class KhataBase(BaseModel):
    person_name: str = Field(min_length=1, max_length=120, examples=["Ahmed"])
    person_phone: str | None = Field(default=None, max_length=32)
    person_email: EmailStr | None = None
    currency: str = Field(default=DEFAULT_KHATA_CURRENCY, min_length=3, max_length=3)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("person_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Give the khata a name.")
        return stripped

    @field_validator("person_phone")
    @classmethod
    def _strip_phone(cls, value: str | None) -> str | None:
        return (value or "").strip() or None

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        return normalise_currency(value)


class KhataCreate(KhataBase):
    person_user_id: uuid.UUID | None = Field(
        default=None,
        description="Link to an account when the other party is on Owsify. Optional.",
    )


class KhataUpdate(BaseModel):
    """All optional — a PATCH. `is_archived` is how a khata is archived or restored."""

    person_name: str | None = Field(default=None, min_length=1, max_length=120)
    person_phone: str | None = Field(default=None, max_length=32)
    person_email: EmailStr | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    notes: str | None = Field(default=None, max_length=2000)
    person_user_id: uuid.UUID | None = None
    is_archived: bool | None = None

    @field_validator("person_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Give the khata a name.")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str | None) -> str | None:
        return normalise_currency(value) if value else value

    @model_validator(mode="after")
    def _something_to_do(self) -> "KhataUpdate":
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        return self


class KhataRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    person_name: str
    display_name: str = Field(
        description="The linked account's name when there is one, else person_name."
    )
    person_phone: str | None
    person_email: str | None
    person_user: UserRead | None = Field(
        default=None, description="Set when the other party has an account."
    )
    currency: str
    notes: str | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    # Derived from the khata's entries, so it cannot drift from them.
    balance: Decimal = Field(
        description="Positive: they owe you. Negative: you owe them. Zero: settled."
    )
    entry_count: int
    last_entry_on: datetime | None = None


class KhataListPage(BaseModel):
    items: list[KhataRead]
    total: int
    limit: int
    offset: int

    # Per-currency, because a user may keep khatas in more than one and adding
    # rupees to dollars would produce a number that means nothing.
    totals: list["KhataCurrencyTotal"]


class KhataCurrencyTotal(BaseModel):
    currency: str
    owed_to_you: Decimal
    you_owe: Decimal
    net: Decimal
    khata_count: int
