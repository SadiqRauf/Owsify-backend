"""Khata: a running two-party ledger.

A khata is **one person's book about one other person** — "Ahmed's khata" is my
record of what passes between Ahmed and me. That makes it a different shape from a
group, and the difference drives two decisions:

**The other party need not have an account.** The whole point of a khata is that a
shopkeeper can keep one for a customer who will never install anything. So a khata
carries a `person_name` of its own and *optionally* links to a `User`. Requiring
the counterparty to sign up first would make the feature useless for its main case.

**It belongs to its owner alone.** Two people who deal with each other each keep
their own khata; neither can see the other's. That is how a paper khata works, and
it avoids inventing a shared-ownership model nobody asked for.
"""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum as SAEnum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.expense import MONEY


class KhataEntryType(StrEnum):
    """Named from the owner's side of the counter, not in accounting terms.

    `GIVEN` and `RECEIVED` need no bookkeeping knowledge to read correctly, where
    debit and credit invert depending on whose books you think you are in.
    """

    GIVEN = "given"
    """You gave them money or goods, so they owe you more."""

    RECEIVED = "received"
    """They paid you back, so they owe you less."""

    ADJUSTMENT = "adjustment"
    """A correction, which may move the balance either way.

    Corrections are their own type rather than a negative `GIVEN` so the ledger
    stays honest about what happened: "I mis-recorded this" is a different event
    from "I handed over money", and a reader scanning the book should be able to
    tell them apart. It is also the one type whose amount may be negative — see
    the constraints on `KhataEntry`.
    """


class KhataAccount(Base, TimestampMixin):
    __tablename__ = "khata_accounts"
    __table_args__ = (
        CheckConstraint("owner_id <> person_user_id", name="no_khata_with_yourself"),
        # One khata per linked person, so a balance can never be split across two
        # books for the same account. Name-only khatas are exempt: two different
        # customers can genuinely share a name.
        Index(
            "uq_khata_accounts_owner_person",
            "owner_id",
            "person_user_id",
            unique=True,
            postgresql_where=text("person_user_id IS NOT NULL"),
        ),
        Index("ix_khata_accounts_owner_archived", "owner_id", "is_archived"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # The counterparty's name as the owner writes it. Always present, even when a
    # user is linked, so the book still reads correctly if that account is deleted.
    person_name: Mapped[str] = mapped_column(String(120), nullable=False)

    # Set only when the other party is on the app. SET NULL rather than CASCADE:
    # losing their account must not delete the owner's ledger.
    person_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )

    person_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    person_email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    currency: Mapped[str] = mapped_column(String(3), default="PKR", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Archiving hides a settled khata without destroying its history, which is what
    # people actually want when they say "remove this".
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    owner: Mapped["User"] = relationship(foreign_keys=[owner_id])  # noqa: F821
    person_user: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[person_user_id], lazy="joined"
    )
    entries: Mapped[list["KhataEntry"]] = relationship(
        back_populates="khata",
        cascade="all, delete-orphan",
        lazy="noload",
        order_by="KhataEntry.entry_date.desc()",
    )

    @property
    def display_name(self) -> str:
        """Prefer the linked account's name, so a rename there is reflected here."""
        return self.person_user.full_name if self.person_user else self.person_name

    def __repr__(self) -> str:
        return f"<KhataAccount {self.person_name} of {self.owner_id}>"


class KhataEntry(Base, TimestampMixin):
    """A single line in a khata.

    The table is created now, with the accounts, because the two are meaningless
    apart: a khata with no way to record a line is an address book. The entry API
    arrives next, but building the schema together means no migration has to
    retrofit a foreign key onto rows that already exist.
    """

    __tablename__ = "khata_entries"
    __table_args__ = (
        # A zero-value line says nothing and would still show up in the book.
        CheckConstraint("amount <> 0", name="amount_is_not_zero"),
        # Direction is carried by entry_type for the two ordinary types, so their
        # amounts stay positive: a negative GIVEN and a positive RECEIVED would be
        # two ways to write the same fact. An ADJUSTMENT is the exception — a
        # correction has no inherent direction, so it carries its own sign.
        CheckConstraint(
            "entry_type = 'adjustment' OR amount > 0",
            name="only_adjustments_may_be_negative",
        ),
        # Entries are read newest-first for one khata, which is the only access
        # pattern the ledger view has.
        Index("ix_khata_entries_khata_date", "khata_id", "entry_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    khata_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("khata_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    entry_type: Mapped[KhataEntryType] = mapped_column(
        SAEnum(
            KhataEntryType,
            native_enum=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )

    # Positive for GIVEN and RECEIVED, where entry_type carries the direction.
    # Signed for ADJUSTMENT, which has no natural direction of its own.
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    created_by_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    khata: Mapped["KhataAccount"] = relationship(back_populates="entries")

    @property
    def signed_amount(self) -> Decimal:
        """Effect on the balance: positive increases what they owe the owner.

        The single definition of what each type does to a balance. The SQL in
        `services/khata.py` mirrors it in a CASE expression because summing has to
        happen in the database, and the two are pinned together by a test rather
        than by hope.
        """
        if self.entry_type is KhataEntryType.GIVEN:
            return self.amount
        if self.entry_type is KhataEntryType.RECEIVED:
            return -self.amount
        # An adjustment is already signed.
        return self.amount
