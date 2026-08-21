"""Note and reminder payloads."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.currencies import normalise_currency
from app.models.note import NoteSubject, ReminderStatus
from app.schemas.user import UserRead


class SubjectRef(BaseModel):
    """What a note or reminder is about, resolved for display.

    Sent alongside the raw ids so a list can be rendered without a second request
    per row just to learn a name.
    """

    kind: NoteSubject
    id: uuid.UUID
    label: str = Field(description="The khata's person, the loan's counterparty, or the person.")
    href: str = Field(description="Where this subject lives in the app.")


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5000, examples=["Will return by September"])

    # Exactly one of these. Enforced in the service with a field error, and by a
    # CHECK constraint in the database.
    khata_id: uuid.UUID | None = None
    loan_id: uuid.UUID | None = None
    person_user_id: uuid.UUID | None = None

    @field_validator("body")
    @classmethod
    def _strip_body(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A note needs something in it.")
        return stripped


class NoteUpdate(BaseModel):
    """Only the body. Re-pointing a note at a different subject would be a new note."""

    body: str = Field(min_length=1, max_length=5000)

    @field_validator("body")
    @classmethod
    def _strip_body(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("A note needs something in it.")
        return stripped


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    body: str
    subject: NoteSubject
    subject_ref: SubjectRef
    khata_id: uuid.UUID | None
    loan_id: uuid.UUID | None
    person_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class NoteListPage(BaseModel):
    items: list[NoteRead]
    total: int
    limit: int
    offset: int


class ReminderBase(BaseModel):
    title: str = Field(min_length=1, max_length=200, examples=["Collect from Ahmed"])
    notes: str | None = Field(default=None, max_length=5000)
    due_date: date = Field(examples=["2026-09-20"])
    remind_on: date | None = Field(
        default=None,
        description=(
            "When to surface it. Must be on or before the due date. Without one, the "
            "reminder surfaces on its due date."
        ),
    )
    amount: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=12,
        decimal_places=2,
        description="For display only — a reminder is a prompt, never a ledger entry.",
    )
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("What is this reminder for?")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str | None) -> str | None:
        return normalise_currency(value) if value else None

    @model_validator(mode="after")
    def _remind_before_due(self) -> "ReminderBase":
        if self.remind_on is not None and self.remind_on > self.due_date:
            raise ValueError("The reminder date must not be after the due date.")
        if self.amount is not None and self.currency is None:
            raise ValueError("An amount needs a currency.")
        return self


class ReminderCreate(ReminderBase):
    khata_id: uuid.UUID | None = None
    loan_id: uuid.UUID | None = None
    person_user_id: uuid.UUID | None = None


class ReminderUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)
    due_date: date | None = None
    remind_on: date | None = None
    amount: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    completed: bool | None = Field(
        default=None,
        description="Maps to a timestamp, so 'when was this done' stays answerable.",
    )

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str | None) -> str | None:
        return normalise_currency(value) if value else None

    @model_validator(mode="after")
    def _something_to_do(self) -> "ReminderUpdate":
        if not self.model_fields_set:
            raise ValueError("Send at least one field to change.")
        return self


class ReminderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    notes: str | None
    due_date: date
    remind_on: date | None
    amount: Decimal | None
    currency: str | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    subject: NoteSubject
    subject_ref: SubjectRef
    khata_id: uuid.UUID | None
    loan_id: uuid.UUID | None
    person_user_id: uuid.UUID | None

    status: ReminderStatus
    days_until_due: int
    is_surfaced: bool = Field(
        description="False while a future remind_on is holding it back."
    )


class ReminderCounts(BaseModel):
    open: int
    surfaced: int
    overdue: int
    due_today: int


class ReminderListPage(BaseModel):
    items: list[ReminderRead]
    total: int
    limit: int
    offset: int
    counts: ReminderCounts = Field(description="Unaffected by the filters on the page.")


class TimelineEntry(BaseModel):
    """One thing that happened, in one person's history.

    Deliberately flat: the timeline merges loans, khata entries, expenses,
    settlements, notes and reminders, and the shape is the intersection of what all
    six can answer.
    """

    id: uuid.UUID
    kind: str = Field(
        description=(
            "loan_given, loan_payment, khata_entry, expense, settlement, note or "
            "reminder."
        )
    )
    occurred_on: date
    occurred_at: datetime
    title: str
    detail: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    href: str | None = Field(default=None, description="Where to go to see more.")


class TimelinePage(BaseModel):
    items: list[TimelineEntry]
    total: int
    limit: int
    offset: int
    person: UserRead | None = None
