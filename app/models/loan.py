"""Loans: a principal, a direction, and the payments that pay it down.

A loan is the same shape as a khata — **one person's record about one other
person** — but it answers a different question. A khata is an open-ended running
tally with no end state; a loan is a fixed principal that is either outstanding or
settled, and it has a date by which it should have been settled.

**A loan has a direction.** You can be the one who lent (`GIVEN`) or the one who
borrowed (`TAKEN`). The row is always owned by whoever is keeping the record, so the
columns are `owner_id` and `counterparty_*` rather than lender and borrower: for a
`TAKEN` loan the owner *is* the borrower, and a column named `lender_id` holding the
borrower would be a schema that lies. That difference is
the whole reason it is not just a khata entry.

**Status is derived, not stored.** The brief lists `status` as a loan field, and
this model deliberately does not have one. Four of the five statuses are facts about
the payments and the calendar — `PAID` means the payments add up to the principal,
`OVERDUE` means the due date has passed with money outstanding — and a stored copy
of a derived fact is a copy that can go stale. A loan whose row says PAID while its
payments sum to less than the principal is a loan nobody can trust. Only
`CANCELLED` is a decision rather than a consequence, so only that one is stored, as
`cancelled_at`. This is the same choice made for every other balance in the app.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Enum as SAEnum,
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


class LoanDirection(StrEnum):
    """Which way the money went, from the record keeper's side."""

    GIVEN = "given"
    """You lent it. They owe you."""

    TAKEN = "taken"
    """You borrowed it. You owe them."""


class LoanStatus(StrEnum):
    """Where a loan stands. Computed by `Loan.status`, never stored."""

    ACTIVE = "active"
    """Outstanding, nothing paid yet, not yet due."""

    PARTIALLY_PAID = "partially_paid"
    """Some paid, some outstanding, not yet due."""

    PAID = "paid"
    """The payments cover the principal."""

    OVERDUE = "overdue"
    """Past its due date with money still outstanding.

    Takes precedence over `PARTIALLY_PAID`: a part-paid loan that is late is late,
    and showing it as merely part-paid would bury the fact that needs acting on.
    """

    OVERPAID = "overpaid"
    """More was repaid than was owed, so the balance has flipped.

    A separate status rather than folding into `PAID`, because the two mean opposite
    things about who owes whom: `PAID` means nobody owes anybody, `OVERPAID` means
    the debt now runs the other way. Calling it paid would hide money that is owed.
    """

    CANCELLED = "cancelled"
    """Written off. The only status that is a decision rather than a consequence,
    and so the only one backed by a stored column."""


class Loan(Base, TimestampMixin):
    __tablename__ = "loans"
    __table_args__ = (
        # A loan of nothing is not a loan. Unlike a khata entry there is no
        # corrective case, so this is a floor rather than a non-zero check.
        CheckConstraint("amount > 0", name="amount_is_positive"),
        CheckConstraint("owner_id <> counterparty_user_id", name="no_loan_to_yourself"),
        # The list is read per owner, filtered by direction and outstanding-ness and
        # sorted by due date. That is the only access pattern the loans page has.
        Index("ix_loans_owner_due", "owner_id", "due_date"),
        Index("ix_loans_owner_direction", "owner_id", "direction"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Whoever is keeping this record, whichever way the money went.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    direction: Mapped[LoanDirection] = mapped_column(
        SAEnum(
            LoanDirection,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=LoanDirection.GIVEN,
        server_default=LoanDirection.GIVEN.value,
        nullable=False,
    )

    # As with a khata, the other party need not have an account: lending to someone
    # who will never install the app is the ordinary case, not an edge one.
    counterparty_name: Mapped[str] = mapped_column(String(120), nullable=False)
    counterparty_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="PKR", nullable=False)

    # Optional: plenty of real lending has no agreed date, and inventing one would
    # make every such loan permanently "overdue" or permanently "active".
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["User"] = relationship(foreign_keys=[owner_id])  # noqa: F821
    counterparty_user: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[counterparty_user_id], lazy="joined"
    )
    payments: Mapped[list["LoanPayment"]] = relationship(
        back_populates="loan",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="LoanPayment.payment_date.desc()",
    )

    @property
    def display_name(self) -> str:
        """Prefer the linked account's name, so a rename there is reflected here."""
        return (
            self.counterparty_user.full_name
            if self.counterparty_user
            else self.counterparty_name
        )

    @property
    def paid(self) -> Decimal:
        """Summed from the payments, which are the only record of what was repaid."""
        return sum((payment.amount for payment in self.payments), Decimal("0.00"))

    @property
    def remaining(self) -> Decimal:
        """What is still owed on the original direction. Never below zero.

        Once the payments cover the principal there is nothing left of the original
        debt — anything beyond it is a new debt the other way, which is what
        `overpaid` and `signed_balance` express.
        """
        return max(self.amount - self.paid, Decimal("0.00"))

    @property
    def overpaid(self) -> Decimal:
        """How much more than the principal came back.

        Repay 1,500 against a 1,000 loan and this is 500 — money that is now owed in
        the opposite direction. Earlier this was swallowed by clamping `remaining`
        at zero, which quietly lost the 500.
        """
        return max(self.paid - self.amount, Decimal("0.00"))

    @property
    def signed_balance(self) -> Decimal:
        """One number for where this loan leaves the two of you.

        **Positive means they owe you**, the same convention as every other balance
        in the app, so loans can be added to khata and group balances without a
        special case.

        A loan you gave starts positive and falls towards zero as it is repaid; if it
        is overpaid it keeps going and turns negative, because you now owe them the
        excess. A loan you took is the mirror image, which is exactly what flipping
        the sign gives.
        """
        outstanding = self.amount - self.paid
        return outstanding if self.direction is LoanDirection.GIVEN else -outstanding

    @property
    def status(self) -> LoanStatus:
        if self.cancelled_at is not None:
            return LoanStatus.CANCELLED
        if self.paid > self.amount:
            return LoanStatus.OVERPAID
        if self.paid == self.amount:
            return LoanStatus.PAID
        if self.due_date is not None and self.due_date < date.today():
            return LoanStatus.OVERDUE
        if self.paid > 0:
            return LoanStatus.PARTIALLY_PAID
        return LoanStatus.ACTIVE

    @property
    def is_outstanding(self) -> bool:
        """Whether the original debt still has anything left on it."""
        return self.cancelled_at is None and self.remaining > 0

    def __repr__(self) -> str:
        return f"<Loan {self.direction.value} {self.amount} {self.counterparty_name}>"


class LoanPayment(Base, TimestampMixin):
    """One repayment against a loan.

    Payments are the source of truth for how much is left, exactly as khata entries
    are for a khata balance. Nothing on the loan row is updated when one is added.
    """

    __tablename__ = "loan_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_is_positive"),
        Index("ix_loan_payments_loan_date", "loan_id", "payment_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    loan_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("loans.id", ondelete="CASCADE"), index=True, nullable=False
    )

    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    loan: Mapped["Loan"] = relationship(back_populates="payments")

    def __repr__(self) -> str:
        return f"<LoanPayment {self.amount} on {self.payment_date}>"
