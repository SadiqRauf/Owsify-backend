from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor
from tests.test_settlements import add_expense, settle


class TestCategorySpending:
    def test_reports_your_share_not_what_you_paid(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        """The distinction that makes the number meaningful."""
        group = make_group(alice, bob)
        add_expense(
            client, alice, group["id"], alice, alice, bob, amount="100.00", category="food"
        )

        alice_rows = client.get("/api/v1/analytics/categories", headers=alice.headers).json()
        assert len(alice_rows) == 1
        # Alice paid 100 but consumed 50.
        assert Decimal(alice_rows[0]["amount"]) == Decimal("50.00")
        assert alice_rows[0]["category"] == "food"

        bob_rows = client.get("/api/v1/analytics/categories", headers=bob.headers).json()
        assert Decimal(bob_rows[0]["amount"]) == Decimal("50.00")

    def test_groups_and_sorts_by_size(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        group = make_group(alice)
        for category, amount in (("food", "10.00"), ("travel", "80.00"), ("food", "20.00")):
            add_expense(
                client, alice, group["id"], alice, alice,
                amount=amount, category=category, description=f"{category} {amount}",
            )

        rows = client.get("/api/v1/analytics/categories", headers=alice.headers).json()

        assert [row["category"] for row in rows] == ["travel", "food"]
        assert Decimal(rows[0]["amount"]) == Decimal("80.00")
        assert Decimal(rows[1]["amount"]) == Decimal("30.00")
        assert rows[1]["expense_count"] == 2

    def test_shares_add_up_to_one_hundred(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        group = make_group(alice)
        for category, amount in (("food", "25.00"), ("travel", "75.00")):
            add_expense(
                client, alice, group["id"], alice, alice,
                amount=amount, category=category, description=category,
            )

        rows = client.get("/api/v1/analytics/categories", headers=alice.headers).json()
        assert sum(Decimal(row["share_of_total"]) for row in rows) == Decimal("100.0")

    def test_date_filtering(self, client: TestClient, alice: Actor, make_group) -> None:
        group = make_group(alice)
        add_expense(
            client, alice, group["id"], alice, alice,
            amount="10.00", expense_date="2026-01-15", description="Old",
        )
        add_expense(
            client, alice, group["id"], alice, alice,
            amount="90.00", expense_date="2026-08-01", description="New",
        )

        recent = client.get(
            "/api/v1/analytics/categories",
            params={"start_date": "2026-06-01"},
            headers=alice.headers,
        ).json()
        assert Decimal(recent[0]["amount"]) == Decimal("90.00")

        old = client.get(
            "/api/v1/analytics/categories",
            params={"end_date": "2026-02-01"},
            headers=alice.headers,
        ).json()
        assert Decimal(old[0]["amount"]) == Decimal("10.00")

    def test_rejects_a_backwards_range(self, client: TestClient, alice: Actor) -> None:
        response = client.get(
            "/api/v1/analytics/categories",
            params={"start_date": "2026-08-01", "end_date": "2026-01-01"},
            headers=alice.headers,
        )
        assert response.status_code == 400

    def test_only_counts_the_requested_currency(
        self, client: TestClient, alice: Actor
    ) -> None:
        usd = client.post(
            "/api/v1/groups", json={"name": "USD", "currency": "USD"}, headers=alice.headers
        ).json()
        eur = client.post(
            "/api/v1/groups", json={"name": "EUR", "currency": "EUR"}, headers=alice.headers
        ).json()
        add_expense(client, alice, usd["id"], alice, alice, amount="100.00")
        add_expense(client, alice, eur["id"], alice, alice, amount="70.00")

        in_usd = client.get("/api/v1/analytics/categories", headers=alice.headers).json()
        assert Decimal(in_usd[0]["amount"]) == Decimal("100.00")

        in_eur = client.get(
            "/api/v1/analytics/categories", params={"currency": "EUR"}, headers=alice.headers
        ).json()
        assert Decimal(in_eur[0]["amount"]) == Decimal("70.00")

    def test_rejects_an_invented_currency(self, client: TestClient, alice: Actor) -> None:
        response = client.get(
            "/api/v1/analytics/categories", params={"currency": "ZZZ"}, headers=alice.headers
        )
        assert response.status_code == 400


class TestMonthlySpending:
    def test_returns_a_continuous_series_including_empty_months(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        group = make_group(alice)
        add_expense(client, alice, group["id"], alice, alice, amount="40.00")

        rows = client.get(
            "/api/v1/analytics/monthly", params={"months": 6}, headers=alice.headers
        ).json()

        assert len(rows) == 6
        # Oldest first, and every month present even with no activity.
        assert rows == sorted(rows, key=lambda row: row["month"])
        assert any(Decimal(row["amount"]) > 0 for row in rows)
        assert all("amount" in row for row in rows)

    def test_respects_the_month_count(self, client: TestClient, alice: Actor) -> None:
        rows = client.get(
            "/api/v1/analytics/monthly", params={"months": 12}, headers=alice.headers
        ).json()
        assert len(rows) == 12

    def test_caps_the_range(self, client: TestClient, alice: Actor) -> None:
        response = client.get(
            "/api/v1/analytics/monthly", params={"months": 99}, headers=alice.headers
        )
        assert response.status_code == 422


class TestGroupStatistics:
    def test_reports_totals_share_and_net(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="100.00")

        rows = client.get("/api/v1/analytics/groups", headers=alice.headers).json()

        assert len(rows) == 1
        stat = rows[0]
        assert Decimal(stat["total_expenses"]) == Decimal("100.00")
        assert Decimal(stat["your_share"]) == Decimal("50.00")
        assert Decimal(stat["your_net"]) == Decimal("50.00")
        assert stat["expense_count"] == 1
        assert stat["member_count"] == 2

    def test_net_agrees_with_the_group_balance_page(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="90.00")
        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="20.00")

        stat = client.get("/api/v1/analytics/groups", headers=alice.headers).json()[0]
        balance = client.get(
            f"/api/v1/balances/groups/{group['id']}", headers=alice.headers
        ).json()

        assert Decimal(stat["your_net"]) == Decimal(balance["your_net"])

    def test_only_your_groups(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        make_group(alice, name="Mine")
        make_group(bob, name="Theirs")

        rows = client.get("/api/v1/analytics/groups", headers=alice.headers).json()
        assert [row["group"]["name"] for row in rows] == ["Mine"]


class TestDashboard:
    def test_returns_everything_in_one_call(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(
            client, alice, group["id"], alice, alice, bob, amount="60.00", category="food"
        )
        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="10.00")

        body = client.get("/api/v1/analytics/dashboard", headers=alice.headers).json()

        assert Decimal(body["total_spent"]) == Decimal("30.00")
        assert body["expense_count"] == 1
        assert body["window"]["currency"] == "USD"

        assert len(body["by_category"]) == 1
        assert len(body["by_month"]) == 6
        assert len(body["by_group"]) == 1
        assert len(body["groups"]) == 1
        assert len(body["recent_expenses"]) == 1
        assert len(body["recent_settlements"]) == 1

        # Bob owed 30 and paid 10, so 20 remains.
        usd = next(row for row in body["balances"] if row["currency"] == "USD")
        assert Decimal(usd["net"]) == Decimal("20.00")

    def test_balances_span_currencies_even_though_spending_does_not(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        usd = client.post(
            "/api/v1/groups",
            json={"name": "USD", "currency": "USD", "member_ids": [bob.id]},
            headers=alice.headers,
        ).json()
        eur = client.post(
            "/api/v1/groups",
            json={"name": "EUR", "currency": "EUR", "member_ids": [bob.id]},
            headers=alice.headers,
        ).json()
        add_expense(client, alice, usd["id"], alice, alice, bob, amount="100.00")
        add_expense(client, alice, eur["id"], alice, alice, bob, amount="80.00")

        body = client.get("/api/v1/analytics/dashboard", headers=alice.headers).json()

        # Spending is one currency...
        assert body["window"]["currency"] == "USD"
        assert Decimal(body["total_spent"]) == Decimal("50.00")

        # ...but balances must not hide the euro position.
        currencies = {row["currency"] for row in body["balances"]}
        assert currencies == {"USD", "EUR"}

    def test_is_empty_but_valid_for_a_new_account(
        self, client: TestClient, alice: Actor
    ) -> None:
        body = client.get("/api/v1/analytics/dashboard", headers=alice.headers).json()

        assert Decimal(body["total_spent"]) == Decimal("0.00")
        assert body["balances"] == []
        assert body["by_category"] == []
        assert body["recent_expenses"] == []
        # The month series is still a full window so the chart has an axis.
        assert len(body["by_month"]) == 6

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/api/v1/analytics/dashboard").status_code == 401
