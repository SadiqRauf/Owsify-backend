"""In-app notifications: something another person did that affects you.

Unlike the activity feed, this is a stored log rather than a derived view. It has
to be: a notification records that something *happened*, including things that no
longer exist (a deleted expense, a group that is gone), and it carries per-person
state (read or not) that no other table has anywhere to keep.

Rows are written in the same transaction as the change they describe, so a change
that rolls back never leaves a notification behind claiming it happened.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class NotificationType(StrEnum):
    EXPENSE_ADDED = "expense_added"
    EXPENSE_UPDATED = "expense_updated"
    EXPENSE_DELETED = "expense_deleted"
    SETTLEMENT_RECORDED = "settlement_recorded"
    SETTLEMENT_UPDATED = "settlement_updated"
    SETTLEMENT_DELETED = "settlement_deleted"
    FRIEND_REQUEST = "friend_request"
    FRIEND_ACCEPTED = "friend_accepted"
    GROUP_ADDED = "group_added"
    GROUP_REMOVED = "group_removed"
    GROUP_ROLE_CHANGED = "group_role_changed"
    GROUP_OWNERSHIP_TRANSFERRED = "group_ownership_transferred"
    GROUP_DELETED = "group_deleted"
    INVITATION_ACCEPTED = "invitation_accepted"


class Notification(Base, TimestampMixin):
    __tablename__ = "notifications"
    __table_args__ = (
        # The bell reads newest first per recipient; that is the main access pattern.
        Index("ix_notifications_recipient_created", "recipient_id", "created_at"),
        # The unread badge is polled, so counting it must not scan read history.
        Index(
            "ix_notifications_recipient_unread",
            "recipient_id",
            postgresql_where="read_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    recipient_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable so a notification outlives the account of the person who caused it.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    type: Mapped[NotificationType] = mapped_column(
        SAEnum(NotificationType, native_enum=False, length=40, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # A snapshot of what the notification is about (names, amounts, ids), taken when
    # it was written. Deliberately not foreign keys: the subject may be deleted, and
    # "Alice deleted Dinner" still has to say "Dinner".
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id], lazy="joined")  # noqa: F821

    @property
    def is_read(self) -> bool:
        return self.read_at is not None
