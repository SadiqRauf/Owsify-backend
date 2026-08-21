"""Notes and reminders — the context around a balance."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.models.note import Note, NoteSubject, Reminder, ReminderStatus
from app.schemas.common import Message
from app.schemas.note import (
    NoteCreate,
    NoteListPage,
    NoteRead,
    NoteUpdate,
    ReminderCounts,
    ReminderCreate,
    ReminderListPage,
    ReminderRead,
    ReminderUpdate,
    SubjectRef,
)
from app.services import note as note_service

notes_router = APIRouter(prefix="/notes", tags=["notes"])
reminders_router = APIRouter(prefix="/reminders", tags=["reminders"])


def _subject_ref(item: Note | Reminder) -> SubjectRef:
    """Resolve the subject to a name and a link.

    Done server-side so a list of twenty notes about twenty different things renders
    from one response instead of twenty follow-up requests.
    """
    if item.khata_id is not None:
        return SubjectRef(
            kind=NoteSubject.KHATA,
            id=item.khata_id,
            label=item.khata.display_name if item.khata else "Khata",
            href=f"/khata/{item.khata_id}",
        )
    if item.loan_id is not None:
        return SubjectRef(
            kind=NoteSubject.LOAN,
            id=item.loan_id,
            label=item.loan.display_name if item.loan else "Loan",
            href=f"/loans/{item.loan_id}",
        )
    return SubjectRef(
        kind=NoteSubject.PERSON,
        id=item.person_user_id,
        label=item.person.full_name if item.person else "Person",
        href=f"/people/{item.person_user_id}",
    )


def _note_read(note: Note) -> NoteRead:
    return NoteRead(
        id=note.id,
        body=note.body,
        subject=note.subject,
        subject_ref=_subject_ref(note),
        khata_id=note.khata_id,
        loan_id=note.loan_id,
        person_user_id=note.person_user_id,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


def _reminder_read(reminder: Reminder) -> ReminderRead:
    return ReminderRead(
        id=reminder.id,
        title=reminder.title,
        notes=reminder.notes,
        due_date=reminder.due_date,
        remind_on=reminder.remind_on,
        amount=reminder.amount,
        currency=reminder.currency,
        completed_at=reminder.completed_at,
        created_at=reminder.created_at,
        updated_at=reminder.updated_at,
        subject=reminder.subject,
        subject_ref=_subject_ref(reminder),
        khata_id=reminder.khata_id,
        loan_id=reminder.loan_id,
        person_user_id=reminder.person_user_id,
        status=reminder.status,
        days_until_due=reminder.days_until_due,
        is_surfaced=reminder.is_surfaced,
    )


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


@notes_router.get("", response_model=NoteListPage, summary="Your notes")
def list_notes(
    db: DbSession,
    current_user: ActiveUser,
    subject: Annotated[NoteSubject | None, Query(description="Only notes on this kind.")] = None,
    khata_id: Annotated[uuid.UUID | None, Query()] = None,
    loan_id: Annotated[uuid.UUID | None, Query()] = None,
    person_user_id: Annotated[uuid.UUID | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NoteListPage:
    """Newest first. A note belongs to exactly one khata, loan or person."""
    notes, total = note_service.list_notes(
        db,
        current_user.id,
        subject=subject,
        khata_id=khata_id,
        loan_id=loan_id,
        person_user_id=person_user_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return NoteListPage(
        items=[_note_read(note) for note in notes], total=total, limit=limit, offset=offset
    )


@notes_router.post(
    "", response_model=NoteRead, status_code=status.HTTP_201_CREATED, summary="Add a note"
)
def create_note(payload: NoteCreate, db: DbSession, current_user: ActiveUser) -> NoteRead:
    """Send exactly one of `khata_id`, `loan_id` or `person_user_id`.

    The subject must be yours: a note on someone else's khata is a 404, the same as
    the khata itself would be.
    """
    return _note_read(note_service.create_note(db, current_user, payload))


@notes_router.patch("/{note_id}", response_model=NoteRead, summary="Edit a note")
def update_note(
    note_id: uuid.UUID, payload: NoteUpdate, db: DbSession, current_user: ActiveUser
) -> NoteRead:
    note = note_service.get_note_or_404(db, note_id, current_user.id)
    return _note_read(note_service.update_note(db, note, payload))


@notes_router.delete("/{note_id}", response_model=Message, summary="Delete a note")
def delete_note(note_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    note = note_service.get_note_or_404(db, note_id, current_user.id)
    note_service.delete_note(db, note)
    return Message(message="Note deleted.")


# --------------------------------------------------------------------------- #
# Reminders
# --------------------------------------------------------------------------- #


@reminders_router.get("", response_model=ReminderListPage, summary="What to chase")
def list_reminders(
    db: DbSession,
    current_user: ActiveUser,
    status_filter: Annotated[ReminderStatus | None, Query(alias="status")] = None,
    subject: Annotated[NoteSubject | None, Query()] = None,
    khata_id: Annotated[uuid.UUID | None, Query()] = None,
    loan_id: Annotated[uuid.UUID | None, Query()] = None,
    person_user_id: Annotated[uuid.UUID | None, Query()] = None,
    surfaced_only: Annotated[
        bool, Query(description="Hide reminders whose remind_on has not arrived.")
    ] = False,
    include_completed: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReminderListPage:
    """Soonest-due first, because this list is a queue of work.

    `counts` describes every open reminder and ignores the filters, so a filtered
    view cannot make an overdue queue look empty.
    """
    reminders, total = note_service.list_reminders(
        db,
        current_user.id,
        status=status_filter,
        subject=subject,
        khata_id=khata_id,
        loan_id=loan_id,
        person_user_id=person_user_id,
        surfaced_only=surfaced_only,
        include_completed=include_completed,
        limit=limit,
        offset=offset,
    )

    return ReminderListPage(
        items=[_reminder_read(reminder) for reminder in reminders],
        total=total,
        limit=limit,
        offset=offset,
        counts=ReminderCounts(**note_service.reminder_counts(db, current_user.id)),
    )


@reminders_router.post(
    "", response_model=ReminderRead, status_code=status.HTTP_201_CREATED, summary="Add a reminder"
)
def create_reminder(
    payload: ReminderCreate, db: DbSession, current_user: ActiveUser
) -> ReminderRead:
    """`due_date` is when the money is expected; `remind_on` is when to surface it.

    They are separate because collapsing them forces a choice between nagging early
    and being told the day something is already late.
    """
    return _reminder_read(note_service.create_reminder(db, current_user, payload))


@reminders_router.patch("/{reminder_id}", response_model=ReminderRead, summary="Edit a reminder")
def update_reminder(
    reminder_id: uuid.UUID, payload: ReminderUpdate, db: DbSession, current_user: ActiveUser
) -> ReminderRead:
    """`completed: true` marks it done, `false` reopens it."""
    reminder = note_service.get_reminder_or_404(db, reminder_id, current_user.id)
    return _reminder_read(note_service.update_reminder(db, reminder, payload))


@reminders_router.delete("/{reminder_id}", response_model=Message, summary="Delete a reminder")
def delete_reminder(reminder_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    reminder = note_service.get_reminder_or_404(db, reminder_id, current_user.id)
    note_service.delete_reminder(db, reminder)
    return Message(message="Reminder deleted.")
