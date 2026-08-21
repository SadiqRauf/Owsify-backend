"""Notes and reminders — the things around a balance rather than the balance itself.

Both attach to *something*: a khata, a loan, or a person. The obvious way to model
that is a generic `subject_type` + `subject_id` pair, and this deliberately does not
do it. A generic pair cannot carry a foreign key, so nothing stops a note pointing
at a khata that was deleted last week, and every read has to branch on a string. Two
nullable foreign keys plus a person column, with a CHECK that exactly one is set,
gets referential integrity from the database and makes "notes on this loan" an
ordinary indexed query.

The cost is one column per attachable kind. With three kinds that is cheaper than
the integrity it buys.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.expense import MONEY

# Exactly one subject per row. Written once and shared, so a note and a reminder can
# never disagree about what "attached to something" means.
_EXACTLY_ONE_SUBJECT = (
    "(CASE WHEN khata_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN loan_id IS NOT NULL THEN 1 ELSE 0 END) + "
    "(CASE WHEN person_user_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


class NoteSubject(StrEnum):
    """What a note or reminder is about. Derived from which column is set."""

    KHATA = "khata"
    LOAN = "loan"
    PERSON = "person"


class Note(Base, TimestampMixin):
    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint(_EXACTLY_ONE_SUBJECT, name="exactly_one_subject"),
        CheckConstraint("length(btrim(body)) > 0", name="body_is_not_blank"),
        Index("ix_notes_owner_created", "owner_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    # CASCADE on khata and loan: a note about a deleted loan has nothing left to be
    # about. SET NULL would leave a subjectless row the CHECK above forbids.
    khata_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("khata_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), index=True, nullable=True
    )
    person_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )

    khata: Mapped["KhataAccount | None"] = relationship(  # noqa: F821
        foreign_keys=[khata_id], lazy="joined"
    )
    loan: Mapped["Loan | None"] = relationship(foreign_keys=[loan_id], lazy="joined")  # noqa: F821
    person: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[person_user_id], lazy="joined"
    )

    @property
    def subject(self) -> NoteSubject:
        if self.khata_id is not None:
            return NoteSubject.KHATA
        if self.loan_id is not None:
            return NoteSubject.LOAN
        return NoteSubject.PERSON

    def __repr__(self) -> str:
        return f"<Note on {self.subject.value}>"


class ReminderStatus(StrEnum):
    """Where a reminder stands. Computed, never stored — see `Reminder.status`."""

    UPCOMING = "upcoming"
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"
    COMPLETED = "completed"


class Reminder(Base, TimestampMixin):
    """Something to chase, on a date.

    Two dates, because they answer different questions. `due_date` is when the money
    is expected; `remind_on` is when the app should surface it. They are usually a
    few days apart, and collapsing them into one would force a choice between
    nagging early and telling someone the day it is already late.
    """

    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint(_EXACTLY_ONE_SUBJECT, name="exactly_one_subject"),
        CheckConstraint("length(btrim(title)) > 0", name="title_is_not_blank"),
        CheckConstraint("amount IS NULL OR amount > 0", name="amount_is_positive"),
        CheckConstraint(
            "remind_on IS NULL OR remind_on <= due_date", name="remind_on_before_due_date"
        ),
        # The upcoming list is read per owner ordered by due date, which is the only
        # access pattern the reminders panel has.
        Index("ix_reminders_owner_due", "owner_id", "due_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    due_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Optional: without one, the reminder simply surfaces on its due date.
    remind_on: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    # Optional, and only for display. A reminder is a prompt, not a ledger entry —
    # this figure is never summed into any balance.
    amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    khata_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("khata_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    loan_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), index=True, nullable=True
    )
    person_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True
    )

    khata: Mapped["KhataAccount | None"] = relationship(  # noqa: F821
        foreign_keys=[khata_id], lazy="joined"
    )
    loan: Mapped["Loan | None"] = relationship(foreign_keys=[loan_id], lazy="joined")  # noqa: F821
    person: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[person_user_id], lazy="joined"
    )

    @property
    def subject(self) -> NoteSubject:
        if self.khata_id is not None:
            return NoteSubject.KHATA
        if self.loan_id is not None:
            return NoteSubject.LOAN
        return NoteSubject.PERSON

    @property
    def status(self) -> ReminderStatus:
        if self.completed_at is not None:
            return ReminderStatus.COMPLETED
        today = date.today()
        if self.due_date < today:
            return ReminderStatus.OVERDUE
        if self.due_date == today:
            return ReminderStatus.DUE_TODAY
        return ReminderStatus.UPCOMING

    @property
    def days_until_due(self) -> int:
        return (self.due_date - date.today()).days

    @property
    def is_surfaced(self) -> bool:
        """Whether this should be showing yet.

        A reminder with a `remind_on` in the future is real but not yet anyone's
        problem — that is the entire point of having the second date.
        """
        if self.completed_at is not None:
            return False
        if self.remind_on is None:
            return True
        return self.remind_on <= date.today()

    def __repr__(self) -> str:
        return f"<Reminder {self.title!r} due {self.due_date}>"
