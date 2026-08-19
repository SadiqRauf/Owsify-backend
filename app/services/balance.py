"""The balance engine.

Everything the app says about who owes whom comes from here.

Three properties are maintained deliberately, because a violation of any of them
shows up as money that has silently appeared or vanished:

1. **Integer cents.** No float ever touches a balance. Decimal in, cents through
   the arithmetic, Decimal out.
2. **Per currency.** Balances are never summed across currencies. Two people can
   simultaneously owe each other in different currencies and both are true; adding
   them would produce a number that means nothing.
3. **Zero sum.** Within one currency and one scope, every ledger sums to zero.
   ``assert_consistent`` checks this and is exercised by the test suite.

An expense creates debt: everyone's share is owed to whoever paid. A settlement
discharges it: paying someone reduces what you owe them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.expense import Expense, ExpenseSplit
from app.models.group import GroupMember
from app.models.settlement import Settlement

ZERO = Decimal("0.00")


def to_cents(amount: Decimal) -> int:
    return int(amount.quantize(Decimal("0.01")) * 100)


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def _pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """A stable ordering for a pair, so one entry represents both directions."""
    return (a, b) if str(a) < str(b) else (b, a)


@dataclass
class Ledger:
    """Net position between every pair of people, per currency, in cents.

    ``entries[currency][(x, y)]`` is what **x owes y**, where ``(x, y)`` is the
    canonical ordering. A negative value therefore means y owes x. Storing one
    signed number per pair rather than two directed ones makes it impossible for
    the two directions to disagree.
    """

    entries: dict[str, dict[tuple[uuid.UUID, uuid.UUID], int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )

    def add(self, debtor: uuid.UUID, creditor: uuid.UUID, cents: int, currency: str) -> None:
        """Record that `debtor` now owes `creditor` a further `cents`."""
        if debtor == creditor or cents == 0:
            return
        key = _pair(debtor, creditor)
        self.entries[currency][key] += cents if key[0] == debtor else -cents

    def net_for(self, user_id: uuid.UUID, currency: str) -> int:
        """Positive when this user is owed money overall in that currency."""
        total = 0
        for (first, second), cents in self.entries.get(currency, {}).items():
            if first == user_id:
                total -= cents
            elif second == user_id:
                total += cents
        return total

    def between(self, user_id: uuid.UUID, other_id: uuid.UUID, currency: str) -> int:
        """Positive when `other_id` owes `user_id`."""
        key = _pair(user_id, other_id)
        cents = self.entries.get(currency, {}).get(key, 0)
        return -cents if key[0] == user_id else cents

    def counterparties(self, user_id: uuid.UUID, currency: str) -> dict[uuid.UUID, int]:
        """Every non-zero position this user holds, keyed by the other person."""
        result: dict[uuid.UUID, int] = {}
        for (first, second), cents in self.entries.get(currency, {}).items():
            if cents == 0:
                continue
            if first == user_id:
                result[second] = -cents
            elif second == user_id:
                result[first] = cents
        return result

    @property
    def currencies(self) -> list[str]:
        return sorted(currency for currency, pairs in self.entries.items() if any(pairs.values()))

    def participants(self, currency: str) -> set[uuid.UUID]:
        people: set[uuid.UUID] = set()
        for (first, second), cents in self.entries.get(currency, {}).items():
            if cents:
                people.update((first, second))
        return people

    def assert_consistent(self) -> None:
        """Every currency's net positions must cancel out exactly.

        Called by the tests rather than in the request path — it is a statement
        about the arithmetic, and if it ever fails the bug is here, not in a caller.
        """
        for currency in self.entries:
            people = self.participants(currency)
            total = sum(self.net_for(person, currency) for person in people)
            if total != 0:
                raise AssertionError(
                    f"Ledger for {currency} does not balance: nets sum to {total} cents"
                )


# --------------------------------------------------------------------------- #
# Building the ledger from stored rows
# --------------------------------------------------------------------------- #
def _visible_expense_filter(user_id: uuid.UUID | None):
    if user_id is None:
        return None
    in_my_group = Expense.group_id.in_(
        select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    )
    i_paid = Expense.paid_by_id == user_id
    i_owe = Expense.id.in_(select(ExpenseSplit.expense_id).where(ExpenseSplit.user_id == user_id))
    return or_(in_my_group, i_paid, i_owe)


def apply_expense(ledger: Ledger, expense: Expense) -> None:
    """Everyone's share is owed to whoever paid."""
    for split in expense.splits:
        if split.user_id == expense.paid_by_id:
            continue
        ledger.add(split.user_id, expense.paid_by_id, to_cents(split.amount), expense.currency)


def apply_settlement(ledger: Ledger, settlement: Settlement) -> None:
    """Paying someone reduces what you owe them, i.e. they now owe you that much."""
    ledger.add(
        settlement.to_user_id,
        settlement.from_user_id,
        to_cents(settlement.amount),
        settlement.currency,
    )


def build_ledger(
    db: Session,
    *,
    viewer_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    include_settlements: bool = True,
) -> Ledger:
    """Replay every relevant expense and settlement into a fresh ledger.

    Recomputed per request rather than kept as a running total on a row: a stored
    balance can drift from the transactions behind it after an edit or a delete,
    and a wrong balance that looks authoritative is worse than a slow one. If this
    ever becomes a bottleneck the fix is a cache with the transactions as the
    source of truth, not a denormalised column.
    """
    ledger = Ledger()

    expense_conditions = []
    settlement_conditions = []

    if group_id is not None:
        expense_conditions.append(Expense.group_id == group_id)
        settlement_conditions.append(Settlement.group_id == group_id)
    elif viewer_id is not None:
        expense_conditions.append(_visible_expense_filter(viewer_id))
        settlement_conditions.append(
            or_(Settlement.from_user_id == viewer_id, Settlement.to_user_id == viewer_id)
        )

    expenses = db.scalars(
        select(Expense).options(selectinload(Expense.splits)).where(*expense_conditions)
    ).all()
    for expense in expenses:
        apply_expense(ledger, expense)

    if include_settlements:
        settlements = db.scalars(select(Settlement).where(*settlement_conditions)).all()
        for settlement in settlements:
            apply_settlement(ledger, settlement)

    return ledger


# --------------------------------------------------------------------------- #
# Reading balances out
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Debt:
    """A directed amount: `debtor` owes `creditor`."""

    debtor_id: uuid.UUID
    creditor_id: uuid.UUID
    amount: Decimal
    currency: str


def who_owes_whom(ledger: Ledger, currency: str | None = None) -> list[Debt]:
    """Flatten the ledger into directed debts, largest first."""
    debts: list[Debt] = []
    currencies = [currency] if currency else ledger.currencies

    for code in currencies:
        for (first, second), cents in ledger.entries.get(code, {}).items():
            if cents > 0:
                debts.append(Debt(first, second, from_cents(cents), code))
            elif cents < 0:
                debts.append(Debt(second, first, from_cents(-cents), code))

    debts.sort(key=lambda debt: (debt.currency, -debt.amount, str(debt.debtor_id)))
    return debts


def net_positions(ledger: Ledger, currency: str) -> dict[uuid.UUID, int]:
    """Each person's overall position in cents. Positive means they are owed."""
    return {
        person: ledger.net_for(person, currency) for person in ledger.participants(currency)
    }


# --------------------------------------------------------------------------- #
# Debt simplification
# --------------------------------------------------------------------------- #
def simplify(ledger: Ledger, currency: str) -> list[Debt]:
    """Reduce a web of debts to the fewest transfers that settle everyone.

    Only the net position of each person matters, so the problem becomes: given
    creditors and debtors that sum to zero, pay everyone off. Repeatedly matching
    the largest debtor with the largest creditor clears at least one person per
    transfer, so at most n-1 transfers are produced for n people.

        Ali owes Sadiq 50, Ahmed owes Ali 30
        nets: Sadiq +50, Ali -20, Ahmed -30
        -> Ahmed pays Sadiq 30, Ali pays Sadiq 20

    Note this deliberately changes *who pays whom*: Ahmed never borrowed from Sadiq
    directly. That is the point of simplification, but it is also why it is offered
    as a separate view rather than replacing the real pairwise balances.
    """
    positions = net_positions(ledger, currency)

    creditors = sorted(
        ((person, cents) for person, cents in positions.items() if cents > 0),
        key=lambda item: (-item[1], str(item[0])),
    )
    debtors = sorted(
        ((person, -cents) for person, cents in positions.items() if cents < 0),
        key=lambda item: (-item[1], str(item[0])),
    )

    transfers: list[Debt] = []
    i = j = 0

    while i < len(debtors) and j < len(creditors):
        debtor, owed = debtors[i]
        creditor, due = creditors[j]

        amount = min(owed, due)
        if amount > 0:
            transfers.append(Debt(debtor, creditor, from_cents(amount), currency))

        owed -= amount
        due -= amount
        debtors[i] = (debtor, owed)
        creditors[j] = (creditor, due)

        # Advance whichever side is now clear. When both are, both advance.
        if owed == 0:
            i += 1
        if due == 0:
            j += 1

    return transfers


def simplify_all(ledger: Ledger) -> list[Debt]:
    """Simplify every currency in the ledger independently."""
    return [debt for currency in ledger.currencies for debt in simplify(ledger, currency)]


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class CurrencySummary:
    currency: str
    owed_to_you: Decimal
    you_owe: Decimal
    net: Decimal


def summarise_for_user(ledger: Ledger, user_id: uuid.UUID) -> list[CurrencySummary]:
    """Totals per currency, from the point of view of one person."""
    summaries: list[CurrencySummary] = []

    for currency in ledger.currencies:
        owed_to_you = 0
        you_owe = 0

        for cents in ledger.counterparties(user_id, currency).values():
            if cents > 0:
                owed_to_you += cents
            else:
                you_owe += -cents

        if owed_to_you or you_owe:
            summaries.append(
                CurrencySummary(
                    currency=currency,
                    owed_to_you=from_cents(owed_to_you),
                    you_owe=from_cents(you_owe),
                    net=from_cents(owed_to_you - you_owe),
                )
            )

    return summaries
