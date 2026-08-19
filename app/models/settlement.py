"""Settlements: a recorded payment from one person to another.

A settlement is not an expense. An expense creates debt and is divided between
people; a settlement discharges debt and always moves money in one direction. They
are kept as separate tables because conflating them makes every balance query have
to remember which rows to treat differently.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Date, Enum as SAEnum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.expense import MONEY


class PaymentMethod(StrEnum):
    CASH = "cash"
    BANK_TRANSFER = "bank_transfer"
    CARD = "card"
    PAYPAL = "paypal"
    VENMO = "venmo"
    UPI = "upi"
    OTHER = "other"


class Settlement(Base, TimestampMixin):
    __tablename__ = "settlements"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_is_positive"),
        CheckConstraint("from_user_id <> to_user_id", name="no_self_settlement"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Null for a settlement between friends outside any group.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), index=True, nullable=True
    )

    # from_user pays to_user, which reduces what from_user owes them.
    from_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    settled_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    method: Mapped[PaymentMethod] = mapped_column(
        SAEnum(
            PaymentMethod,
            native_enum=False,
            length=24,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=PaymentMethod.CASH,
        nullable=False,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    group: Mapped["Group | None"] = relationship()  # noqa: F821
    from_user: Mapped["User"] = relationship(foreign_keys=[from_user_id], lazy="joined")  # noqa: F821
    to_user: Mapped["User"] = relationship(foreign_keys=[to_user_id], lazy="joined")  # noqa: F821
    created_by: Mapped["User"] = relationship(foreign_keys=[created_by_id], lazy="joined")  # noqa: F821

    def involves(self, user_id: uuid.UUID) -> bool:
        return user_id in (self.from_user_id, self.to_user_id)

    def __repr__(self) -> str:
        return f"<Settlement {self.amount} {self.currency} {self.from_user_id}->{self.to_user_id}>"


# Index for the common "settlements between these two people" lookup.
sa.Index(
    "ix_settlements_pair",
    Settlement.from_user_id,
    Settlement.to_user_id,
)
