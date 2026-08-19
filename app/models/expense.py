"""Expenses and the per-person splits that divide them."""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum as SAEnum,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

# Money is stored as exact decimal, never float: 12 digits with 2 after the point
# covers any realistic shared expense without rounding drift.
MONEY = Numeric(12, 2)


class SplitType(StrEnum):
    EQUAL = "equal"
    EXACT = "exact"
    PERCENTAGE = "percentage"


class ExpenseCategory(StrEnum):
    GENERAL = "general"
    FOOD = "food"
    GROCERIES = "groceries"
    RENT = "rent"
    UTILITIES = "utilities"
    TRANSPORT = "transport"
    ENTERTAINMENT = "entertainment"
    TRAVEL = "travel"
    SHOPPING = "shopping"
    HEALTH = "health"
    OTHER = "other"


def _enum_column(enum_cls: type[StrEnum], **kwargs: object):
    return mapped_column(
        SAEnum(
            enum_cls,
            native_enum=False,
            length=24,
            values_callable=lambda e: [m.value for m in e],
        ),
        **kwargs,
    )


class Expense(Base, TimestampMixin):
    __tablename__ = "expenses"
    __table_args__ = (CheckConstraint("amount > 0", name="amount_is_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Null means a one-to-one expense between friends rather than a group expense.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), index=True, nullable=True
    )

    description: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    category: Mapped[ExpenseCategory] = _enum_column(
        ExpenseCategory, default=ExpenseCategory.GENERAL, nullable=False
    )
    split_type: Mapped[SplitType] = _enum_column(SplitType, default=SplitType.EQUAL, nullable=False)

    paid_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    group: Mapped["Group | None"] = relationship(back_populates="expenses")  # noqa: F821
    paid_by: Mapped["User"] = relationship(foreign_keys=[paid_by_id], lazy="joined")  # noqa: F821
    created_by: Mapped["User"] = relationship(foreign_keys=[created_by_id], lazy="joined")  # noqa: F821
    splits: Mapped[list["ExpenseSplit"]] = relationship(
        back_populates="expense",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def participant_ids(self) -> set[uuid.UUID]:
        return {split.user_id for split in self.splits}

    def share_for(self, user_id: uuid.UUID) -> Decimal:
        """What this user owes on this expense, before accounting for who paid."""
        return next(
            (split.amount for split in self.splits if split.user_id == user_id),
            Decimal("0.00"),
        )


class ExpenseSplit(Base, TimestampMixin):
    """One participant's share of one expense."""

    __tablename__ = "expense_splits"
    __table_args__ = (
        UniqueConstraint("expense_id", "user_id", name="uq_expense_splits_expense_user"),
        CheckConstraint("amount >= 0", name="share_is_not_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    expense_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("expenses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # Only set for percentage splits, so an edit form can show what was entered
    # rather than the money it resolved to.
    percentage: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)

    expense: Mapped["Expense"] = relationship(back_populates="splits")
    user: Mapped["User"] = relationship(lazy="joined")  # noqa: F821
