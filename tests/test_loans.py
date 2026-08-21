"""Loans: a fixed principal, paid down by its payments.

The theme of these tests is that `paid`, `remaining` and `status` are consequences
of the payment rows and of the calendar — never stored, never settable directly.
Every test that changes the payments asserts the derived figures moved with them.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor

TODAY = date.today()


def give_loan(client: TestClient, actor: Actor, **extra) -> dict:
    body = {"counterparty_name": "Ahmed", "amount": "50000.00", "currency": "PKR"} | extra
    response = client.post("/api/v1/loans", json=body, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()


def pay(client: TestClient, actor: Actor, loan_id: str, amount: str, **extra) -> dict:
    body = {"amount": amount, "payment_date": TODAY.isoformat()} | extra
    response = client.post(
        f"/api/v1/loans/{loan_id}/payments", json=body, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def read(client: TestClient, actor: Actor, loan_id: str) -> dict:
    response = client.get(f"/api/v1/loans/{loan_id}", headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()


class TestTheWorkedExample:
    def test_fifty_thousand_less_twenty_leaves_thirty(
        self, client: TestClient, alice: Actor
    ) -> None:
        """The example from the brief, end to end."""
        loan = give_loan(client, alice)
        assert Decimal(loan["remaining"]) == Decimal("50000.00")
        assert loan["status"] == "active"

        pay(client, alice, loan["id"], "20000.00")

        body = read(client, alice, loan["id"])
        assert Decimal(body["amount"]) == Decimal("50000.00")
        assert Decimal(body["paid"]) == Decimal("20000.00")
        assert Decimal(body["remaining"]) == Decimal("30000.00")
        assert body["status"] == "partially_paid"


class TestStatusIsDerived:
    def test_a_new_loan_is_active(self, client: TestClient, alice: Actor) -> None:
        assert give_loan(client, alice)["status"] == "active"

    def test_a_part_payment_makes_it_partially_paid(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "1.00")
        assert read(client, alice, loan["id"])["status"] == "partially_paid"

    def test_paying_the_whole_amount_makes_it_paid(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "50000.00")
        body = read(client, alice, loan["id"])
        assert body["status"] == "paid"
        assert Decimal(body["remaining"]) == Decimal("0.00")

    def test_a_past_due_date_makes_it_overdue(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice, due_date=(TODAY - timedelta(days=1)).isoformat())
        assert read(client, alice, loan["id"])["status"] == "overdue"

    def test_overdue_beats_partially_paid(self, client: TestClient, alice: Actor) -> None:
        """A late part-paid loan is late. Showing it as merely part-paid buries that."""
        loan = give_loan(client, alice, due_date=(TODAY - timedelta(days=3)).isoformat())
        pay(client, alice, loan["id"], "10000.00")
        assert read(client, alice, loan["id"])["status"] == "overdue"

    def test_a_settled_loan_is_not_overdue(self, client: TestClient, alice: Actor) -> None:
        """Nothing is left to be late for."""
        loan = give_loan(client, alice, due_date=(TODAY - timedelta(days=3)).isoformat())
        pay(client, alice, loan["id"], "50000.00")
        body = read(client, alice, loan["id"])
        assert body["status"] == "paid"
        assert body["days_until_due"] is None

    def test_a_future_due_date_is_counted_down(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice, due_date=(TODAY + timedelta(days=5)).isoformat())
        assert read(client, alice, loan["id"])["days_until_due"] == 5

    def test_an_overdue_loan_counts_negative(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice, due_date=(TODAY - timedelta(days=2)).isoformat())
        assert read(client, alice, loan["id"])["days_until_due"] == -2

    def test_a_loan_with_no_due_date_has_no_countdown(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Inventing a date would make every such loan permanently active or late."""
        loan = give_loan(client, alice)
        assert loan["due_date"] is None
        assert loan["days_until_due"] is None
        assert loan["status"] == "active"

    def test_deleting_a_payment_puts_the_money_back(
        self, client: TestClient, alice: Actor
    ) -> None:
        """The clearest proof that nothing is stored."""
        loan = give_loan(client, alice)
        detail = pay(client, alice, loan["id"], "50000.00")
        assert detail["status"] == "paid"

        payment_id = detail["payments"][0]["id"]
        removed = client.delete(f"/api/v1/loans/payments/{payment_id}", headers=alice.headers)
        assert removed.status_code == 200, removed.text

        body = read(client, alice, loan["id"])
        assert Decimal(body["paid"]) == Decimal("0.00")
        assert Decimal(body["remaining"]) == Decimal("50000.00")
        assert body["status"] == "active"

    def test_editing_a_payment_moves_the_remaining(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice)
        detail = pay(client, alice, loan["id"], "20000.00")
        payment_id = detail["payments"][0]["id"]

        updated = client.patch(
            f"/api/v1/loans/payments/{payment_id}",
            json={"amount": "35000.00"},
            headers=alice.headers,
        )
        assert updated.status_code == 200, updated.text
        assert Decimal(updated.json()["remaining"]) == Decimal("15000.00")


class TestPartialPayments:
    def test_many_partial_payments_add_up(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        for _ in range(5):
            pay(client, alice, loan["id"], "1000.00")

        body = read(client, alice, loan["id"])
        assert Decimal(body["paid"]) == Decimal("5000.00")
        assert Decimal(body["remaining"]) == Decimal("45000.00")
        assert body["payment_count"] == 5

    def test_an_overpayment_flips_the_balance(self, client: TestClient, alice: Actor) -> None:
        """Repay more than was owed and the excess is owed back the other way.

        The worked case: a 1,000 loan repaid with 1,500 leaves you owing 500.
        """
        loan = give_loan(client, alice, amount="1000.00")
        pay(client, alice, loan["id"], "1500.00")

        body = read(client, alice, loan["id"])
        assert Decimal(body["paid"]) == Decimal("1500.00")
        # Nothing left of the original debt...
        assert Decimal(body["remaining"]) == Decimal("0.00")
        # ...and 500 now runs the other way.
        assert Decimal(body["overpaid"]) == Decimal("500.00")
        assert Decimal(body["signed_balance"]) == Decimal("-500.00")
        assert body["status"] == "overpaid"

    def test_overpaid_is_not_reported_as_paid(self, client: TestClient, alice: Actor) -> None:
        """The two mean opposite things about who owes whom."""
        settled = give_loan(client, alice, amount="1000.00")
        pay(client, alice, settled["id"], "1000.00")
        assert read(client, alice, settled["id"])["status"] == "paid"

        over = give_loan(client, alice, amount="1000.00")
        pay(client, alice, over["id"], "1000.01")
        assert read(client, alice, over["id"])["status"] == "overpaid"

    def test_removing_the_overpayment_puts_it_back(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice, amount="1000.00")
        pay(client, alice, loan["id"], "600.00")
        extra = pay(client, alice, loan["id"], "900.00")

        assert read(client, alice, loan["id"])["status"] == "overpaid"

        payment_id = next(
            p["id"] for p in extra["payments"] if Decimal(p["amount"]) == Decimal("900.00")
        )
        client.delete(f"/api/v1/loans/payments/{payment_id}", headers=alice.headers)

        body = read(client, alice, loan["id"])
        assert Decimal(body["overpaid"]) == Decimal("0.00")
        assert Decimal(body["signed_balance"]) == Decimal("400.00")

    def test_the_payment_list_shows_what_was_left_after_each(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Dated a day apart on purpose.

        The whole suite runs inside one transaction, so Postgres `now()` — and
        therefore every `created_at` — is identical. Two payments on the same date
        then have no true chronological order, and the id tiebreak that keeps the
        sequence *stable* is arbitrary rather than meaningful. Distinct dates ask
        the question the running total actually answers.
        """
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "10000.00", payment_date=(TODAY - timedelta(days=1)).isoformat())
        pay(client, alice, loan["id"], "15000.00")

        page = client.get(
            f"/api/v1/loans/{loan['id']}/payments", headers=alice.headers
        ).json()

        # Newest first: 50000 - 25000, then 50000 - 10000.
        assert [Decimal(item["remaining_after"]) for item in page["items"]] == [
            Decimal("25000.00"),
            Decimal("40000.00"),
        ]
        assert Decimal(page["paid"]) == Decimal("25000.00")
        assert Decimal(page["remaining"]) == Decimal("25000.00")

    def test_remaining_after_survives_paging(self, client: TestClient, alice: Actor) -> None:
        """Summed per page it would restart at the page boundary."""
        loan = give_loan(client, alice, amount="1000.00")
        for _ in range(4):
            pay(client, alice, loan["id"], "100.00")

        second = client.get(
            f"/api/v1/loans/{loan['id']}/payments",
            params={"limit": 2, "offset": 2},
            headers=alice.headers,
        ).json()

        # The two oldest payments: 1000-200 and 1000-100.
        assert [Decimal(i["remaining_after"]) for i in second["items"]] == [
            Decimal("800.00"),
            Decimal("900.00"),
        ]


class TestMarkPaid:
    def test_settle_records_the_payment_that_closes_it(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Marking paid writes a real payment, so the history explains the change."""
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "20000.00")

        settled = client.post(f"/api/v1/loans/{loan['id']}/settle", headers=alice.headers)
        assert settled.status_code == 200, settled.text
        body = settled.json()

        assert body["status"] == "paid"
        assert Decimal(body["remaining"]) == Decimal("0.00")
        assert body["payment_count"] == 2
        # And the closing payment is exactly what was left.
        closing = [Decimal(p["amount"]) for p in body["payments"]]
        assert Decimal("30000.00") in closing

    def test_settling_a_settled_loan_is_a_conflict(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "50000.00")
        again = client.post(f"/api/v1/loans/{loan['id']}/settle", headers=alice.headers)
        assert again.status_code == 409


class TestCancellation:
    def test_cancelling_keeps_the_payments(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "20000.00")

        cancelled = client.delete(f"/api/v1/loans/{loan['id']}", headers=alice.headers)
        assert cancelled.status_code == 200, cancelled.text

        body = read(client, alice, loan["id"])
        assert body["status"] == "cancelled"
        assert Decimal(body["paid"]) == Decimal("20000.00")

    def test_a_cancelled_loan_refuses_a_payment(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        client.delete(f"/api/v1/loans/{loan['id']}", headers=alice.headers)

        refused = client.post(
            f"/api/v1/loans/{loan['id']}/payments",
            json={"amount": "1.00", "payment_date": TODAY.isoformat()},
            headers=alice.headers,
        )
        assert refused.status_code == 409

    def test_a_cancelled_loan_can_be_reopened(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        client.delete(f"/api/v1/loans/{loan['id']}", headers=alice.headers)

        reopened = client.patch(
            f"/api/v1/loans/{loan['id']}", json={"status": "active"}, headers=alice.headers
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["status"] == "active"

    def test_permanent_delete_destroys_it(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        gone = client.delete(
            f"/api/v1/loans/{loan['id']}", params={"permanent": True}, headers=alice.headers
        )
        assert gone.status_code == 200
        assert client.get(f"/api/v1/loans/{loan['id']}", headers=alice.headers).status_code == 404


class TestValidation:
    def test_a_loan_of_nothing_is_refused(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/loans",
            json={"counterparty_name": "Ahmed", "amount": "0.00"},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_a_negative_loan_is_refused(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/loans",
            json={"counterparty_name": "Ahmed", "amount": "-100.00"},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_a_nameless_loan_is_refused(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/loans",
            json={"counterparty_name": "   ", "amount": "100.00"},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_you_cannot_lend_to_yourself(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/loans",
            json={"counterparty_name": "Me", "amount": "100.00", "counterparty_user_id": alice.id},
            headers=alice.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "self_loan"

    def test_a_future_payment_is_refused(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        response = client.post(
            f"/api/v1/loans/{loan['id']}/payments",
            json={"amount": "1.00", "payment_date": (TODAY + timedelta(days=7)).isoformat()},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_the_principal_cannot_drop_below_what_was_repaid(
        self, client: TestClient, alice: Actor
    ) -> None:
        """That would describe a loan its own history contradicts."""
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "20000.00")

        response = client.patch(
            f"/api/v1/loans/{loan['id']}", json={"amount": "5000.00"}, headers=alice.headers
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "below_paid"

    def test_the_principal_can_be_corrected_above_what_was_repaid(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "20000.00")
        response = client.patch(
            f"/api/v1/loans/{loan['id']}", json={"amount": "25000.00"}, headers=alice.headers
        )
        assert response.status_code == 200
        assert Decimal(response.json()["remaining"]) == Decimal("5000.00")


class TestListing:
    def test_soonest_due_comes_first_and_undated_last(
        self, client: TestClient, alice: Actor
    ) -> None:
        give_loan(client, alice, counterparty_name="No date")
        give_loan(
            client, alice, counterparty_name="Later", due_date=(TODAY + timedelta(days=30)).isoformat()
        )
        give_loan(
            client, alice, counterparty_name="Sooner", due_date=(TODAY + timedelta(days=2)).isoformat()
        )

        page = client.get("/api/v1/loans", headers=alice.headers).json()
        assert [item["counterparty_name"] for item in page["items"]] == ["Sooner", "Later", "No date"]

    def test_filtering_by_status(self, client: TestClient, alice: Actor) -> None:
        active = give_loan(client, alice, counterparty_name="Active")
        part = give_loan(client, alice, counterparty_name="Part")
        pay(client, alice, part["id"], "100.00")

        page = client.get(
            "/api/v1/loans", params={"status": "partially_paid"}, headers=alice.headers
        ).json()
        assert [item["counterparty_name"] for item in page["items"]] == ["Part"]
        assert page["total"] == 1

        assert active["status"] == "active"

    def test_searching_by_name_or_description(self, client: TestClient, alice: Actor) -> None:
        give_loan(client, alice, counterparty_name="Usman", description="Bike repair")
        give_loan(client, alice, counterparty_name="Ahmed", description="Rent")

        by_name = client.get(
            "/api/v1/loans", params={"search": "usman"}, headers=alice.headers
        ).json()
        assert [i["counterparty_name"] for i in by_name["items"]] == ["Usman"]

        by_note = client.get(
            "/api/v1/loans", params={"search": "bike"}, headers=alice.headers
        ).json()
        assert [i["counterparty_name"] for i in by_note["items"]] == ["Usman"]

    def test_totals_are_per_currency_and_ignore_the_filters(
        self, client: TestClient, alice: Actor
    ) -> None:
        """A filtered view must not make an outstanding book look clear."""
        pkr = give_loan(client, alice, amount="50000.00", currency="PKR")
        pay(client, alice, pkr["id"], "20000.00")
        give_loan(client, alice, amount="100.00", currency="USD")

        page = client.get(
            "/api/v1/loans", params={"status": "paid"}, headers=alice.headers
        ).json()
        assert page["items"] == []

        totals = {bucket["currency"]: bucket for bucket in page["totals"]}
        assert Decimal(totals["PKR"]["lent"]) == Decimal("50000.00")
        assert Decimal(totals["PKR"]["repaid_to_you"]) == Decimal("20000.00")
        assert Decimal(totals["PKR"]["receivable"]) == Decimal("30000.00")
        assert Decimal(totals["USD"]["receivable"]) == Decimal("100.00")

    def test_a_cancelled_loan_leaves_the_outstanding_total(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice)
        client.delete(f"/api/v1/loans/{loan['id']}", headers=alice.headers)

        page = client.get("/api/v1/loans", headers=alice.headers).json()
        totals = {bucket["currency"]: bucket for bucket in page["totals"]}
        assert Decimal(totals["PKR"]["receivable"]) == Decimal("0.00")
        assert Decimal(totals["PKR"]["payable"]) == Decimal("0.00")
        assert totals["PKR"]["loan_count"] == 1

    def test_overdue_is_reported_in_the_totals(self, client: TestClient, alice: Actor) -> None:
        give_loan(client, alice, due_date=(TODAY - timedelta(days=1)).isoformat())
        page = client.get("/api/v1/loans", headers=alice.headers).json()
        totals = {bucket["currency"]: bucket for bucket in page["totals"]}
        assert Decimal(totals["PKR"]["overdue"]) == Decimal("50000.00")
        assert totals["PKR"]["overdue_count"] == 1


class TestThePersonPage:
    def test_a_linked_loan_lands_in_the_unified_total(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        loan = give_loan(
            client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id, currency="USD"
        )
        pay(client, alice, loan["id"], "10000.00")

        summary = client.get(
            f"/api/v1/people/{bob.id}/summary", headers=alice.headers
        ).json()

        assert Decimal(summary["balances"]["loan_balance"]) == Decimal("40000.00")
        assert Decimal(summary["balances"]["total_balance"]) == Decimal("40000.00")
        assert summary["loan_count"] == 1

    def test_a_cancelled_loan_is_not_money_you_expect_back(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        loan = give_loan(
            client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id, currency="USD"
        )
        client.delete(f"/api/v1/loans/{loan['id']}", headers=alice.headers)

        summary = client.get(
            f"/api/v1/people/{bob.id}/summary", headers=alice.headers
        ).json()
        assert Decimal(summary["balances"]["loan_balance"]) == Decimal("0.00")

    def test_a_loan_in_another_currency_is_not_added_in(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        give_loan(
            client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id, currency="PKR"
        )

        usd = client.get(f"/api/v1/people/{bob.id}/summary", headers=alice.headers).json()
        assert Decimal(usd["balances"]["loan_balance"]) == Decimal("0.00")

        pkr = client.get(
            f"/api/v1/people/{bob.id}/summary", params={"currency": "PKR"}, headers=alice.headers
        ).json()
        assert Decimal(pkr["balances"]["loan_balance"]) == Decimal("50000.00")


class TestPermissions:
    def test_someone_elses_loan_is_a_404(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        """404 rather than 403: confirming it exists would leak who Alice lends to."""
        loan = give_loan(client, alice)
        assert client.get(f"/api/v1/loans/{loan['id']}", headers=bob.headers).status_code == 404
        assert (
            client.patch(
                f"/api/v1/loans/{loan['id']}", json={"amount": "1.00"}, headers=bob.headers
            ).status_code
            == 404
        )
        assert client.delete(f"/api/v1/loans/{loan['id']}", headers=bob.headers).status_code == 404

    def test_you_cannot_pay_someone_elses_loan(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        loan = give_loan(client, alice)
        response = client.post(
            f"/api/v1/loans/{loan['id']}/payments",
            json={"amount": "1.00", "payment_date": TODAY.isoformat()},
            headers=bob.headers,
        )
        assert response.status_code == 404

    def test_you_cannot_touch_someone_elses_payment(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        loan = give_loan(client, alice)
        detail = pay(client, alice, loan["id"], "100.00")
        payment_id = detail["payments"][0]["id"]

        assert (
            client.delete(
                f"/api/v1/loans/payments/{payment_id}", headers=bob.headers
            ).status_code
            == 404
        )

    def test_loans_need_a_token(self, client: TestClient) -> None:
        assert client.get("/api/v1/loans").status_code == 401
        assert client.post("/api/v1/loans", json={}).status_code == 401

    def test_an_unknown_loan_is_a_404(self, client: TestClient, alice: Actor) -> None:
        assert (
            client.get(f"/api/v1/loans/{uuid.uuid4()}", headers=alice.headers).status_code == 404
        )


class TestRouting:
    def test_the_payment_route_is_not_read_as_a_loan_id(
        self, client: TestClient, alice: Actor
    ) -> None:
        """`/loans/payments/{id}` must not be matched by `/loans/{loan_id}`."""
        response = client.delete(
            f"/api/v1/loans/payments/{uuid.uuid4()}", headers=alice.headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "Payment not found."


class TestDirection:
    """A loan can be one you gave or one you took."""

    def test_a_loan_defaults_to_one_you_gave(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        assert loan["direction"] == "given"
        assert Decimal(loan["signed_balance"]) == Decimal("50000.00")

    def test_a_loan_you_took_owes_the_other_way(self, client: TestClient, alice: Actor) -> None:
        """Same row, mirrored sign: you owe them, so the balance is negative."""
        loan = give_loan(client, alice, direction="taken", amount="8000.00")

        assert loan["direction"] == "taken"
        assert Decimal(loan["remaining"]) == Decimal("8000.00")
        assert Decimal(loan["signed_balance"]) == Decimal("-8000.00")

    def test_repaying_a_loan_you_took_walks_the_balance_to_zero(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice, direction="taken", amount="8000.00")
        pay(client, alice, loan["id"], "3000.00")

        body = read(client, alice, loan["id"])
        assert Decimal(body["remaining"]) == Decimal("5000.00")
        assert Decimal(body["signed_balance"]) == Decimal("-5000.00")
        assert body["status"] == "partially_paid"

    def test_overpaying_a_loan_you_took_flips_it_in_your_favour(
        self, client: TestClient, alice: Actor
    ) -> None:
        """The mirror of the given case: overpay what you owed and they owe you."""
        loan = give_loan(client, alice, direction="taken", amount="1000.00")
        pay(client, alice, loan["id"], "1500.00")

        body = read(client, alice, loan["id"])
        assert Decimal(body["overpaid"]) == Decimal("500.00")
        assert Decimal(body["signed_balance"]) == Decimal("500.00")
        assert body["status"] == "overpaid"

    def test_filtering_by_direction(self, client: TestClient, alice: Actor) -> None:
        give_loan(client, alice, counterparty_name="Lent to")
        give_loan(client, alice, counterparty_name="Borrowed from", direction="taken")

        given = client.get(
            "/api/v1/loans", params={"direction": "given"}, headers=alice.headers
        ).json()
        taken = client.get(
            "/api/v1/loans", params={"direction": "taken"}, headers=alice.headers
        ).json()

        assert [i["counterparty_name"] for i in given["items"]] == ["Lent to"]
        assert [i["counterparty_name"] for i in taken["items"]] == ["Borrowed from"]

    def test_the_two_directions_are_totalled_separately(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Owed 50,000 while owing 30,000 is not the same as being owed 20,000."""
        give_loan(client, alice, amount="50000.00")
        give_loan(client, alice, amount="30000.00", direction="taken")

        totals = {
            bucket["currency"]: bucket
            for bucket in client.get("/api/v1/loans", headers=alice.headers).json()["totals"]
        }

        assert Decimal(totals["PKR"]["lent"]) == Decimal("50000.00")
        assert Decimal(totals["PKR"]["borrowed"]) == Decimal("30000.00")
        assert Decimal(totals["PKR"]["receivable"]) == Decimal("50000.00")
        assert Decimal(totals["PKR"]["payable"]) == Decimal("30000.00")
        assert Decimal(totals["PKR"]["net"]) == Decimal("20000.00")

    def test_the_direction_can_be_corrected(self, client: TestClient, alice: Actor) -> None:
        """Picking the wrong way round is an easy mistake and a cheap fix."""
        loan = give_loan(client, alice, amount="900.00")
        response = client.patch(
            f"/api/v1/loans/{loan['id']}", json={"direction": "taken"}, headers=alice.headers
        )
        assert response.status_code == 200, response.text
        assert Decimal(response.json()["signed_balance"]) == Decimal("-900.00")

    def test_settle_works_in_both_directions(self, client: TestClient, alice: Actor) -> None:
        taken = give_loan(client, alice, direction="taken", amount="700.00")
        settled = client.post(f"/api/v1/loans/{taken['id']}/settle", headers=alice.headers)

        assert settled.status_code == 200, settled.text
        assert settled.json()["status"] == "paid"
        assert Decimal(settled.json()["signed_balance"]) == Decimal("0.00")


class TestDirectionOnThePersonPage:
    def test_a_loan_you_took_is_negative_in_the_unified_total(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        give_loan(
            client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id,
            currency="USD", direction="taken", amount="400.00",
        )

        summary = client.get(f"/api/v1/people/{bob.id}/summary", headers=alice.headers).json()
        assert Decimal(summary["balances"]["loan_balance"]) == Decimal("-400.00")

    def test_both_directions_net_off(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        for direction, amount in (("given", "1000.00"), ("taken", "400.00")):
            give_loan(
                client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id,
                currency="USD", direction=direction, amount=amount,
            )

        summary = client.get(f"/api/v1/people/{bob.id}/summary", headers=alice.headers).json()
        assert Decimal(summary["balances"]["loan_balance"]) == Decimal("600.00")
        assert summary["loan_count"] == 2

    def test_an_overpayment_shows_up_as_money_you_owe_them(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        """The headline case, end to end: 1,000 lent, 1,500 back, you owe 500."""
        befriend(alice, bob)
        loan = give_loan(
            client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id,
            currency="USD", amount="1000.00",
        )
        pay(client, alice, loan["id"], "1500.00")

        summary = client.get(f"/api/v1/people/{bob.id}/summary", headers=alice.headers).json()
        assert Decimal(summary["balances"]["loan_balance"]) == Decimal("-500.00")
        assert Decimal(summary["balances"]["total_balance"]) == Decimal("-500.00")


class TestDirectionScopedTotals:
    """The three figures the loans page leads with."""

    def test_given_taken_and_net(self, client: TestClient, alice: Actor) -> None:
        give_loan(client, alice, amount="50000.00")
        pay(client, alice, client.get("/api/v1/loans", headers=alice.headers).json()["items"][0]["id"], "20000.00")
        give_loan(client, alice, amount="30000.00", direction="taken")

        totals = {
            b["currency"]: b
            for b in client.get("/api/v1/loans", headers=alice.headers).json()["totals"]
        }["PKR"]

        assert Decimal(totals["given_balance"]) == Decimal("30000.00")
        assert Decimal(totals["taken_balance"]) == Decimal("-30000.00")
        assert Decimal(totals["net"]) == Decimal("0.00")

    def test_an_overpaid_given_loan_stays_under_given(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Scoped by direction, not by sign.

        An overpaid loan you gave is money you owe *on a loan you gave*. Letting it
        drift into the taken column would make the two bricks describe something
        other than their labels.
        """
        loan = give_loan(client, alice, amount="1000.00")
        pay(client, alice, loan["id"], "1500.00")

        totals = {
            b["currency"]: b
            for b in client.get("/api/v1/loans", headers=alice.headers).json()["totals"]
        }["PKR"]

        assert Decimal(totals["given_balance"]) == Decimal("-500.00")
        assert Decimal(totals["taken_balance"]) == Decimal("0.00")
        # Sign-scoped totals put the same 500 under payable, which is also correct —
        # the two answer different questions.
        assert Decimal(totals["payable"]) == Decimal("500.00")
        assert Decimal(totals["receivable"]) == Decimal("0.00")

    def test_the_three_bricks_always_add_up(self, client: TestClient, alice: Actor) -> None:
        give_loan(client, alice, amount="700.00")
        give_loan(client, alice, amount="250.00", direction="taken")
        over = give_loan(client, alice, amount="100.00", direction="taken")
        pay(client, alice, over["id"], "180.00")

        totals = {
            b["currency"]: b
            for b in client.get("/api/v1/loans", headers=alice.headers).json()["totals"]
        }["PKR"]

        assert Decimal(totals["given_balance"]) + Decimal(totals["taken_balance"]) == Decimal(
            totals["net"]
        )
