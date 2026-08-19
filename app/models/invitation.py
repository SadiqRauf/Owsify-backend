"""Invitations sent to people who do not have an account yet."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class InvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"


class Invitation(Base, TimestampMixin):
    """An emailed invite that becomes a friendship once the invitee signs up.

    There is no unique constraint on (invited_by_id, email): re-inviting after a
    cancellation is legitimate, so the "one pending invite per pair" rule lives in
    the service, where it can distinguish a duplicate from a resend.
    """

    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    invited_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Random, unguessable, and unique: it is what identifies the invite in a link.
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    status: Mapped[InvitationStatus] = mapped_column(
        SAEnum(
            InvitationStatus,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=InvitationStatus.PENDING,
        nullable=False,
        index=True,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    invited_by: Mapped["User"] = relationship(foreign_keys=[invited_by_id], lazy="joined")  # noqa: F821

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)

    @property
    def is_open(self) -> bool:
        """Still waiting on the invitee, and still within its lifetime."""
        return self.status is InvitationStatus.PENDING and not self.is_expired
