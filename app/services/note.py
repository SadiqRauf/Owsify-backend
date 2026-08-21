"""Notes and reminders.

Both are owned by one person and attached to exactly one subject. The shared work —
validating that the subject exists and is yours, and turning a filter into a query —
lives here rather than being written twice.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, UnprocessableEntityError
from app.models.khata import KhataAccount
from app.models.loan import Loan
from app.models.note import Note, NoteSubject, Reminder, ReminderStatus
from app.models.user import User
from app.schemas.note import (
    NoteCreate,
    NoteUpdate,
    ReminderCreate,
    ReminderUpdate,
)


def _resolve_subject(
    db: Session,
    owner: User,
    *,
    khata_id: uuid.UUID | None,
    loan_id: uuid.UUID | None,
    person_user_id: uuid.UUID | None,
) -> dict:
    """Check that exactly one subject was given, that it exists, and that it is yours.

    The database enforces "exactly one" too, but a CHECK violation surfaces as a 500
    with nothing to act on. This is where the user gets told which field to fix.
    """
    given = [
        name
        for name, value in (
            ("khata_id", khata_id),
            ("loan_id", loan_id),
            ("person_user_id", person_user_id),
        )
        if value is not None
    ]

    if len(given) != 1:
        raise UnprocessableEntityError(
            "Attach this to exactly one thing: a khata, a loan, or a person.",
            details=[
                {
                    "field": given[1] if len(given) > 1 else "khata_id",
                    "message": (
                        "Give exactly one of khata_id, loan_id or person_user_id."
                        if not given
                        else "Only one subject is allowed."
                    ),
                    "type": "subject_count",
                }
            ],
        )

    if khata_id is not None:
        khata = db.scalar(
            select(KhataAccount).where(
                KhataAccount.id == khata_id, KhataAccount.owner_id == owner.id
            )
        )
        if khata is None:
            raise NotFoundError("Khata not found.")

    if loan_id is not None:
        loan = db.scalar(select(Loan).where(Loan.id == loan_id, Loan.owner_id == owner.id))
        if loan is None:
            raise NotFoundError("Loan not found.")

    if person_user_id is not None:
        if person_user_id == owner.id:
            raise UnprocessableEntityError(
                "Attach this to someone else.",
                details=[
                    {
                        "field": "person_user_id",
                        "message": "You cannot keep notes about yourself here.",
                        "type": "self_subject",
                    }
                ],
            )
        person = db.get(User, person_user_id)
        if person is None or not person.is_active:
            raise NotFoundError("Person not found.")

    return {"khata_id": khata_id, "loan_id": loan_id, "person_user_id": person_user_id}


def _subject_filter(
    statement: Select,
    model: type[Note] | type[Reminder],
    *,
    subject: NoteSubject | None,
    khata_id: uuid.UUID | None,
    loan_id: uuid.UUID | None,
    person_user_id: uuid.UUID | None,
) -> Select:
    if khata_id is not None:
        statement = statement.where(model.khata_id == khata_id)
    if loan_id is not None:
        statement = statement.where(model.loan_id == loan_id)
    if person_user_id is not None:
        statement = statement.where(model.person_user_id == person_user_id)

    if subject is NoteSubject.KHATA:
        statement = statement.where(model.khata_id.isnot(None))
    elif subject is NoteSubject.LOAN:
        statement = statement.where(model.loan_id.isnot(None))
    elif subject is NoteSubject.PERSON:
        statement = statement.where(model.person_user_id.isnot(None))

    return statement


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


def get_note_or_404(db: Session, note_id: uuid.UUID, owner_id: uuid.UUID) -> Note:
    note = db.scalar(select(Note).where(Note.id == note_id, Note.owner_id == owner_id))
    if note is None:
        raise NotFoundError("Note not found.")
    return note


def list_notes(
    db: Session,
    owner_id: uuid.UUID,
    *,
    subject: NoteSubject | None = None,
    khata_id: uuid.UUID | None = None,
    loan_id: uuid.UUID | None = None,
    person_user_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Note], int]:
    statement = select(Note).where(Note.owner_id == owner_id)
    statement = _subject_filter(
        statement,
        Note,
        subject=subject,
        khata_id=khata_id,
        loan_id=loan_id,
        person_user_id=person_user_id,
    )

    if search:
        statement = statement.where(Note.body.ilike(f"%{search.strip()}%"))

    rows = list(db.scalars(statement.order_by(Note.created_at.desc(), Note.id.desc())))
    return rows[offset : offset + limit], len(rows)


def create_note(db: Session, owner: User, payload: NoteCreate) -> Note:
    subject = _resolve_subject(
        db,
        owner,
        khata_id=payload.khata_id,
        loan_id=payload.loan_id,
        person_user_id=payload.person_user_id,
    )

    note = Note(owner_id=owner.id, body=payload.body, **subject)
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


def update_note(db: Session, note: Note, payload: NoteUpdate) -> Note:
    """Only the body is editable.

    Moving a note to a different subject is not an edit, it is a different note —
    and allowing it would mean re-validating the new subject on every PATCH for a
    case nobody asks for.
    """
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(note, field, value)

    db.commit()
    db.refresh(note)
    return note


def delete_note(db: Session, note: Note) -> None:
    db.delete(note)
    db.commit()


# --------------------------------------------------------------------------- #
# Reminders
# --------------------------------------------------------------------------- #


def get_reminder_or_404(db: Session, reminder_id: uuid.UUID, owner_id: uuid.UUID) -> Reminder:
    reminder = db.scalar(
        select(Reminder).where(Reminder.id == reminder_id, Reminder.owner_id == owner_id)
    )
    if reminder is None:
        raise NotFoundError("Reminder not found.")
    return reminder


def list_reminders(
    db: Session,
    owner_id: uuid.UUID,
    *,
    status: ReminderStatus | None = None,
    subject: NoteSubject | None = None,
    khata_id: uuid.UUID | None = None,
    loan_id: uuid.UUID | None = None,
    person_user_id: uuid.UUID | None = None,
    surfaced_only: bool = False,
    include_completed: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Reminder], int]:
    """Soonest-due first, because a reminder list is a queue of work.

    Status is filtered in Python: three of the four values are facts about today's
    date, and restating them as SQL predicates would duplicate `Reminder.status` in
    a second language where the two could drift apart.
    """
    statement = select(Reminder).where(Reminder.owner_id == owner_id)
    statement = _subject_filter(
        statement,
        Reminder,
        subject=subject,
        khata_id=khata_id,
        loan_id=loan_id,
        person_user_id=person_user_id,
    )

    if not include_completed and status is not ReminderStatus.COMPLETED:
        statement = statement.where(Reminder.completed_at.is_(None))

    rows = list(
        db.scalars(statement.order_by(Reminder.due_date, Reminder.created_at, Reminder.id))
    )

    if status is not None:
        rows = [reminder for reminder in rows if reminder.status is status]
    if surfaced_only:
        rows = [reminder for reminder in rows if reminder.is_surfaced]

    return rows[offset : offset + limit], len(rows)


def create_reminder(db: Session, owner: User, payload: ReminderCreate) -> Reminder:
    subject = _resolve_subject(
        db,
        owner,
        khata_id=payload.khata_id,
        loan_id=payload.loan_id,
        person_user_id=payload.person_user_id,
    )

    reminder = Reminder(
        owner_id=owner.id,
        title=payload.title,
        notes=payload.notes,
        due_date=payload.due_date,
        remind_on=payload.remind_on,
        amount=payload.amount,
        currency=payload.currency,
        **subject,
    )
    db.add(reminder)
    db.commit()
    db.refresh(reminder)
    return reminder


def update_reminder(db: Session, reminder: Reminder, payload: ReminderUpdate) -> Reminder:
    data = payload.model_dump(exclude_unset=True)

    # `completed` is a verb, not a column: it maps to setting or clearing a
    # timestamp, so that "when was this done" is answerable and not just "was it".
    completed = data.pop("completed", None)

    # Validated against the *prospective* values, before anything is assigned.
    # Raising after mutating would leave the session holding a dirty object that a
    # later flush still tries to write, so the order here is load-bearing.
    due_date = data.get("due_date", reminder.due_date)
    remind_on = data.get("remind_on", reminder.remind_on)

    # A due date moved earlier can strand a remind_on that was valid when it was
    # set, so the pair is re-checked rather than only the incoming field.
    if remind_on is not None and remind_on > due_date:
        raise UnprocessableEntityError(
            "The reminder date must not be after the due date.",
            details=[
                {
                    "field": "remind_on",
                    "message": "Pick a date on or before the due date.",
                    "type": "remind_after_due",
                }
            ],
        )

    for field, value in data.items():
        setattr(reminder, field, value)

    if completed is True and reminder.completed_at is None:
        reminder.completed_at = datetime.now(timezone.utc)
    elif completed is False:
        reminder.completed_at = None

    db.commit()
    db.refresh(reminder)
    return reminder


def delete_reminder(db: Session, reminder: Reminder) -> None:
    db.delete(reminder)
    db.commit()


def reminder_counts(db: Session, owner_id: uuid.UUID) -> dict[str, int]:
    """How many are waiting, so a nav badge does not need the whole list."""
    reminders = list(
        db.scalars(
            select(Reminder).where(Reminder.owner_id == owner_id, Reminder.completed_at.is_(None))
        )
    )

    surfaced = [reminder for reminder in reminders if reminder.is_surfaced]
    return {
        "open": len(reminders),
        "surfaced": len(surfaced),
        "overdue": sum(1 for r in surfaced if r.status is ReminderStatus.OVERDUE),
        "due_today": sum(1 for r in surfaced if r.status is ReminderStatus.DUE_TODAY),
    }
