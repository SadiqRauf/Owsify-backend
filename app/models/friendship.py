"""Friendships, modelled as a directed request that becomes a mutual link."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Enum as SAEnum, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class FriendshipStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Friendship(Base, TimestampMixin):
    """One row per pair. Direction is kept so the UI can show who asked whom."""

    __tablename__ = "friendships"
    __table_args__ = (
        UniqueConstraint("requester_id", "addressee_id", name="uq_friendships_pair"),
        CheckConstraint("requester_id <> addressee_id", name="no_self_friendship"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    requester_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    addressee_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    status: Mapped[FriendshipStatus] = mapped_column(
        SAEnum(FriendshipStatus, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        default=FriendshipStatus.PENDING,
        nullable=False,
        index=True,
    )
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    requester: Mapped["User"] = relationship(foreign_keys=[requester_id], lazy="joined")  # noqa: F821
    addressee: Mapped["User"] = relationship(foreign_keys=[addressee_id], lazy="joined")  # noqa: F821

    def other_user_id(self, viewer_id: uuid.UUID) -> uuid.UUID:
        return self.addressee_id if self.requester_id == viewer_id else self.requester_id

    def other_user(self, viewer_id: uuid.UUID) -> "User":  # noqa: F821
        return self.addressee if self.requester_id == viewer_id else self.requester
