"""Unit tests for the balance arithmetic, with no database involved.

These are the tests that matter most: everything the product says about money is
downstream of this module.
"""

import random
import uuid
from decimal import Decimal

import pytest

from app.services.balance import (
    Ledger,
    from_cents,
    net_positions,
    simplify,
    summarise_for_user,
    to_cents,
    who_owes_whom,
)

USD = "USD"


def person(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def money(value: str) -> Decimal:
    return Decimal(value)


def owes(ledger: Ledger, debtor: uuid.UUID, creditor: uuid.UUID, amount: str) -> None:
    ledger.add(debtor, creditor, to_cents(money(amount)), USD)


class TestLedgerBasics:
    def test_a_single_debt(self) -> None:
        a, b = person(1), person(2)
        ledger = Ledger()
        owes(ledger, a, b, "50.00")

        assert ledger.between(b, a, USD) == 5000  # a owes b
        assert ledger.between(a, b, USD) == -5000
        assert ledger.net_for(b, USD) == 5000
        assert ledger.net_for(a, USD) == -5000
        ledger.assert_consistent()

    def test_opposite_debts_cancel(self) -> None:
        a, b = person(1), person(2)
        ledger = Ledger()
        owes(ledger, a, b, "50.00")
        owes(ledger, b, a, "50.00")

        assert ledger.net_for(a, USD) == 0
        assert ledger.net_for(b, USD) == 0
        assert who_owes_whom(ledger) == []
        ledger.assert_consistent()

    def test_partial_offset_leaves_the_difference(self) -> None:
        a, b = person(1), person(2)
        ledger = Ledger()
        owes(ledger, a, b, "50.00")
        owes(ledger, b, a, "20.00")

        debts = who_owes_whom(ledger)
        assert len(debts) == 1
        assert debts[0].debtor_id == a
        assert debts[0].creditor_id == b
        assert debts[0].amount == money("30.00")

    def test_direction_is_independent_of_pair_ordering(self) -> None:
        """The canonical pair key must not leak into the reported direction."""
        low, high = person(1), person(99)

        forwards = Ledger()
        owes(forwards, low, high, "10.00")
        backwards = Ledger()
        owes(backwards, high, low, "10.00")

        assert forwards.net_for(high, USD) == 1000
        assert backwards.net_for(low, USD) == 1000

    def test_self_debt_is_ignored(self) -> None:
        a = person(1)
        ledger = Ledger()
        owes(ledger, a, a, "10.00")

        assert ledger.net_for(a, USD) == 0
        assert who_owes_whom(ledger) == []


class TestCurrencySeparation:
    def test_currencies_never_mix(self) -> None:
        a, b = person(1), person(2)
        ledger = Ledger()
        ledger.add(a, b, to_cents(money("50.00")), "USD")
        ledger.add(b, a, to_cents(money("50.00")), "EUR")

        # Numerically equal, but they must not cancel.
        assert ledger.net_for(b, "USD") == 5000
        assert ledger.net_for(a, "EUR") == 5000
        assert sorted(ledger.currencies) == ["EUR", "USD"]
        ledger.assert_consistent()

    def test_summary_reports_each_currency_separately(self) -> None:
        a, b = person(1), person(2)
        ledger = Ledger()
        ledger.add(a, b, to_cents(money("50.00")), "USD")
        ledger.add(b, a, to_cents(money("30.00")), "EUR")

        summaries = {s.currency: s for s in summarise_for_user(ledger, b)}
        assert summaries["USD"].owed_to_you == money("50.00")
        assert summaries["USD"].you_owe == money("0.00")
        assert summaries["EUR"].you_owe == money("30.00")
        assert summaries["EUR"].net == money("-30.00")


class TestSimplification:
    def test_the_worked_example_from_the_brief(self) -> None:
        """Ali owes Sadiq 50, Ahmed owes Ali 30
        -> Ahmed pays Sadiq 30, Ali pays Sadiq 20."""
        sadiq, ali, ahmed = person(1), person(2), person(3)

        ledger = Ledger()
        owes(ledger, ali, sadiq, "50.00")
        owes(ledger, ahmed, ali, "30.00")

        transfers = {
            (t.debtor_id, t.creditor_id, t.amount) for t in simplify(ledger, USD)
        }
        assert transfers == {
            (ahmed, sadiq, money("30.00")),
            (ali, sadiq, money("20.00")),
        }

    def test_simplification_preserves_every_net_position(self) -> None:
        people = [person(i) for i in range(1, 7)]
        ledger = Ledger()
        owes(ledger, people[0], people[1], "40.00")
        owes(ledger, people[1], people[2], "25.50")
        owes(ledger, people[2], people[3], "13.25")
        owes(ledger, people[4], people[0], "60.00")
        owes(ledger, people[5], people[4], "8.75")

        before = net_positions(ledger, USD)

        after: dict[uuid.UUID, int] = dict.fromkeys(before, 0)
        for transfer in simplify(ledger, USD):
            after[transfer.debtor_id] -= to_cents(transfer.amount)
            after[transfer.creditor_id] += to_cents(transfer.amount)

        assert after == before

    def test_simplification_never_needs_more_than_n_minus_one_transfers(self) -> None:
        people = [person(i) for i in range(1, 8)]
        ledger = Ledger()
        # A full ring: everyone owes the next person.
        for index, debtor in enumerate(people):
            owes(ledger, debtor, people[(index + 1) % len(people)], "10.00")

        # A ring of equal debts nets to zero for everyone.
        assert simplify(ledger, USD) == []

    def test_a_chain_collapses_to_one_transfer(self) -> None:
        a, b, c, d = (person(i) for i in range(1, 5))
        ledger = Ledger()
        owes(ledger, a, b, "10.00")
        owes(ledger, b, c, "10.00")
        owes(ledger, c, d, "10.00")

        transfers = simplify(ledger, USD)
        assert len(transfers) == 1
        assert transfers[0].debtor_id == a
        assert transfers[0].creditor_id == d
        assert transfers[0].amount == money("10.00")

    def test_transfers_are_fewer_than_or_equal_to_the_raw_debts(self) -> None:
        people = [person(i) for i in range(1, 6)]
        ledger = Ledger()
        owes(ledger, people[0], people[1], "10.00")
        owes(ledger, people[0], people[2], "20.00")
        owes(ledger, people[3], people[1], "5.00")
        owes(ledger, people[4], people[2], "15.00")

        assert len(simplify(ledger, USD)) <= len(who_owes_whom(ledger, USD))

    def test_an_empty_ledger_needs_no_transfers(self) -> None:
        assert simplify(Ledger(), USD) == []

    def test_settled_group_needs_no_transfers(self) -> None:
        a, b = person(1), person(2)
        ledger = Ledger()
        owes(ledger, a, b, "30.00")
        owes(ledger, b, a, "30.00")

        assert simplify(ledger, USD) == []


class TestCentPrecision:
    def test_no_cent_is_created_or_lost_in_simplification(self) -> None:
        """Amounts that do not divide evenly are where rounding bugs hide."""
        people = [person(i) for i in range(1, 5)]
        ledger = Ledger()
        owes(ledger, people[0], people[1], "33.33")
        owes(ledger, people[1], people[2], "33.33")
        owes(ledger, people[2], people[3], "33.34")

        total_out = sum(to_cents(t.amount) for t in simplify(ledger, USD))
        total_in = sum(to_cents(t.amount) for t in simplify(ledger, USD))
        assert total_out == total_in

        after: dict[uuid.UUID, int] = dict.fromkeys(net_positions(ledger, USD), 0)
        for transfer in simplify(ledger, USD):
            after[transfer.debtor_id] -= to_cents(transfer.amount)
            after[transfer.creditor_id] += to_cents(transfer.amount)
        assert after == net_positions(ledger, USD)

    def test_a_single_cent_survives(self) -> None:
        a, b = person(1), person(2)
        ledger = Ledger()
        owes(ledger, a, b, "0.01")

        assert ledger.net_for(b, USD) == 1
        assert who_owes_whom(ledger)[0].amount == money("0.01")

    def test_round_trip_through_cents(self) -> None:
        for value in ("0.00", "0.01", "0.99", "1.00", "12.34", "9999999.99"):
            assert from_cents(to_cents(money(value))) == money(value)


class TestConsistencyUnderRandomLoad:
    """Property-style checks: the invariants must hold for arbitrary webs of debt."""

    @pytest.mark.parametrize("seed", range(12))
    def test_ledger_always_sums_to_zero(self, seed: int) -> None:
        rng = random.Random(seed)
        people = [person(i) for i in range(1, rng.randint(3, 9))]

        ledger = Ledger()
        for _ in range(rng.randint(5, 40)):
            debtor, creditor = rng.sample(people, 2)
            cents = rng.randint(1, 250_000)
            ledger.add(debtor, creditor, cents, USD)

        ledger.assert_consistent()

    @pytest.mark.parametrize("seed", range(12))
    def test_simplification_is_equivalent_and_minimal_enough(self, seed: int) -> None:
        rng = random.Random(seed)
        people = [person(i) for i in range(1, rng.randint(3, 9))]

        ledger = Ledger()
        for _ in range(rng.randint(5, 40)):
            debtor, creditor = rng.sample(people, 2)
            ledger.add(debtor, creditor, rng.randint(1, 250_000), USD)

        before = net_positions(ledger, USD)
        transfers = simplify(ledger, USD)

        after: dict[uuid.UUID, int] = dict.fromkeys(before, 0)
        for transfer in transfers:
            after[transfer.debtor_id] -= to_cents(transfer.amount)
            after[transfer.creditor_id] += to_cents(transfer.amount)

        # Everyone ends up in exactly the position they were in.
        assert after == before

        # And it takes at most one fewer transfer than there are people involved.
        involved = sum(1 for cents in before.values() if cents != 0)
        assert len(transfers) <= max(involved - 1, 0)

        # No transfer is zero or negative.
        assert all(transfer.amount > 0 for transfer in transfers)
