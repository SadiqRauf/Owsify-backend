"""Groups and their membership rows."""

import uuid
from enum import StrEnum

from sqlalchemy import Enum as SAEnum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class GroupRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

    @property
    def can_manage_members(self) -> bool:
        return self in (GroupRole.OWNER, GroupRole.ADMIN)

    @property
    def can_edit_group(self) -> bool:
        return self in (GroupRole.OWNER, GroupRole.ADMIN)


def _role_column(**kwargs: object) -> Mapped[GroupRole]:
    return mapped_column(
        SAEnum(GroupRole, native_enum=False, length=16, values_callable=lambda e: [m.value for m in e]),
        **kwargs,
    )


class Group(Base, TimestampMixin):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(8), nullable=True)

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    members: Mapped[list["GroupMember"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="GroupMember.created_at",
    )
    expenses: Mapped[list["Expense"]] = relationship(  # noqa: F821
        back_populates="group", cascade="all, delete-orphan", lazy="noload"
    )

    def membership_for(self, user_id: uuid.UUID) -> "GroupMember | None":
        return next((m for m in self.members if m.user_id == user_id), None)


class GroupMember(Base, TimestampMixin):
    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_group_members_group_user"),
        # "which groups is this user in" is on the hot path of every visibility
        # check, so index the user side first.
        Index("ix_group_members_user_group", "user_id", "group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[GroupRole] = _role_column(default=GroupRole.MEMBER, nullable=False)

    group: Mapped["Group"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(lazy="joined")  # noqa: F821
