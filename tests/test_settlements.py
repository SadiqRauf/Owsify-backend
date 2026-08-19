from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor

TODAY = "2026-08-01"


def add_expense(client, actor, group_id, payer, *participants, amount="60.00", **extra):
    body = {
        "group_id": group_id,
        "description": extra.pop("description", "Dinner"),
        "amount": amount,
        "expense_date": TODAY,
        "paid_by_id": payer.id,
        "split_type": "equal",
        "splits": [{"user_id": p.id} for p in participants],
    } | extra
    response = client.post("/api/v1/expenses", json=body, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()


def settle(client, actor, *, group_id, payer, payee, amount, **extra):
    body = {
        "group_id": group_id,
        "from_user_id": payer.id,
        "to_user_id": payee.id,
        "amount": amount,
        "settled_on": TODAY,
        "method": "cash",
    } | extra
    return client.post("/api/v1/settlements", json=body, headers=actor.headers)


def net_of(client, actor, group_id) -> Decimal:
    body = client.get(f"/api/v1/balances/groups/{group_id}", headers=actor.headers).json()
    return Decimal(body["your_net"])


class TestCreateSettlement:
    def test_records_a_payment_and_moves_the_balance(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="60.00")

        # Bob owes Alice 30.
        assert net_of(client, alice, group["id"]) == Decimal("30.00")

        response = settle(
            client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00"
        )
        assert response.status_code == 201
        assert response.json()["from_user"]["id"] == bob.id
        assert response.json()["to_user"]["id"] == alice.id

        # And now they are square.
        assert net_of(client, alice, group["id"]) == Decimal("0.00")
        assert net_of(client, bob, group["id"]) == Decimal("0.00")

    def test_a_partial_payment_leaves_the_remainder(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="100.00")

        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="20.00")

        assert net_of(client, alice, group["id"]) == Decimal("30.00")

    def test_overpaying_reverses_the_debt(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="60.00")

        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="50.00")

        # Bob owed 30 and paid 50, so Alice now owes him 20.
        assert net_of(client, alice, group["id"]) == Decimal("-20.00")
        assert net_of(client, bob, group["id"]) == Decimal("20.00")

    def test_takes_the_groups_currency(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        group = client.post(
            "/api/v1/groups",
            json={"name": "EU", "currency": "EUR", "member_ids": [bob.id]},
            headers=alice.headers,
        ).json()

        response = settle(
            client,
            alice,
            group_id=group["id"],
            payer=alice,
            payee=bob,
            amount="10.00",
            currency="USD",
        )
        assert response.json()["currency"] == "EUR"

    def test_between_friends_without_a_group(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        response = settle(
            client, alice, group_id=None, payer=alice, payee=bob, amount="15.00"
        )
        assert response.status_code == 201
        assert response.json()["group_id"] is None

    def test_cannot_settle_with_a_stranger(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        response = settle(client, alice, group_id=None, payer=alice, payee=bob, amount="15.00")
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "not_a_friend"

    def test_cannot_fabricate_a_payment_between_two_other_people(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, befriend
    ) -> None:
        befriend(bob, carol)
        response = settle(client, alice, group_id=None, payer=bob, payee=carol, amount="10.00")
        assert response.status_code == 403

    def test_both_people_must_be_in_the_group(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = settle(
            client, alice, group_id=group["id"], payer=alice, payee=carol, amount="10.00"
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "not_a_member"

    def test_rejects_self_settlement(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        group = make_group(alice)
        response = settle(
            client, alice, group_id=group["id"], payer=alice, payee=alice, amount="10.00"
        )
        assert response.status_code in (400, 422)

    def test_rejects_zero_and_negative(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        for amount in ("0.00", "-5.00"):
            response = settle(
                client, alice, group_id=group["id"], payer=alice, payee=bob, amount=amount
            )
            assert response.status_code == 422, amount

    def test_rejects_a_future_date(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = settle(
            client,
            alice,
            group_id=group["id"],
            payer=alice,
            payee=bob,
            amount="5.00",
            settled_on="2099-01-01",
        )
        assert response.status_code == 422


class TestSettlementHistory:
    def test_lists_with_paging_and_sorting(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        for amount in ("10.00", "30.00", "20.00"):
            settle(client, alice, group_id=group["id"], payer=alice, payee=bob, amount=amount)

        page = client.get(
            "/api/v1/settlements",
            params={"sort": "-amount", "limit": 2},
            headers=alice.headers,
        ).json()

        assert page["total"] == 3
        assert [item["amount"] for item in page["items"]] == ["30.00", "20.00"]

        ascending = client.get(
            "/api/v1/settlements", params={"sort": "amount"}, headers=alice.headers
        ).json()
        assert ascending["items"][0]["amount"] == "10.00"

    def test_filter_by_group_and_person(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        first = make_group(alice, bob, name="One")
        second = make_group(alice, carol, name="Two")
        settle(client, alice, group_id=first["id"], payer=alice, payee=bob, amount="10.00")
        settle(client, alice, group_id=second["id"], payer=alice, payee=carol, amount="20.00")

        by_group = client.get(
            "/api/v1/settlements", params={"group_id": first["id"]}, headers=alice.headers
        ).json()
        assert by_group["total"] == 1

        by_person = client.get(
            "/api/v1/settlements", params={"with_user_id": carol.id}, headers=alice.headers
        ).json()
        assert by_person["total"] == 1
        assert by_person["items"][0]["amount"] == "20.00"

    def test_an_outsider_sees_nothing(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        settle(client, alice, group_id=group["id"], payer=alice, payee=bob, amount="10.00")

        assert client.get("/api/v1/settlements", headers=carol.headers).json()["total"] == 0


class TestEditAndDelete:
    def test_editing_the_amount_moves_the_balance(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="60.00")
        created = settle(
            client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00"
        ).json()

        assert net_of(client, alice, group["id"]) == Decimal("0.00")

        client.patch(
            f"/api/v1/settlements/{created['id']}",
            json={"amount": "10.00"},
            headers=bob.headers,
        )
        assert net_of(client, alice, group["id"]) == Decimal("20.00")

    def test_deleting_restores_the_debt(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="60.00")
        created = settle(
            client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00"
        ).json()

        assert net_of(client, alice, group["id"]) == Decimal("0.00")

        response = client.delete(f"/api/v1/settlements/{created['id']}", headers=bob.headers)
        assert response.status_code == 200
        assert net_of(client, alice, group["id"]) == Decimal("30.00")

    def test_a_bystander_cannot_edit(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        created = settle(
            client, alice, group_id=group["id"], payer=alice, payee=bob, amount="10.00"
        ).json()

        response = client.patch(
            f"/api/v1/settlements/{created['id']}",
            json={"amount": "1.00"},
            headers=carol.headers,
        )
        assert response.status_code == 403


class TestGroupBalanceView:
    def test_reports_totals_members_and_debts(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        add_expense(client, alice, group["id"], alice, alice, bob, carol, amount="90.00")
        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00")

        body = client.get(f"/api/v1/balances/groups/{group['id']}", headers=alice.headers).json()

        assert Decimal(body["total_expenses"]) == Decimal("90.00")
        assert Decimal(body["total_settled"]) == Decimal("30.00")
        assert Decimal(body["your_net"]) == Decimal("30.00")

        nets = {member["user"]["id"]: Decimal(member["net"]) for member in body["members"]}
        assert nets[alice.id] == Decimal("30.00")
        assert nets[bob.id] == Decimal("0.00")
        assert nets[carol.id] == Decimal("-30.00")

        # Every member's net must cancel out.
        assert sum(nets.values()) == Decimal("0.00")

        debtors = {debt["debtor"]["id"] for debt in body["debts"]}
        assert debtors == {carol.id}

    def test_simplified_plan_needs_fewer_transfers(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="50.00",
                    description="A")
        add_expense(client, bob, group["id"], bob, bob, carol, amount="30.00",
                    description="B")

        plan = client.get(
            f"/api/v1/balances/groups/{group['id']}/simplified", headers=alice.headers
        ).json()

        assert plan["transfer_count"] <= plan["original_count"]
        assert plan["currency"] == "USD"

    def test_a_non_member_cannot_read_group_balances(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice)
        response = client.get(f"/api/v1/balances/groups/{group['id']}", headers=bob.headers)
        assert response.status_code == 404


class TestOverallBalances:
    def test_separates_currencies(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        usd_group = client.post(
            "/api/v1/groups",
            json={"name": "USD", "currency": "USD", "member_ids": [bob.id]},
            headers=alice.headers,
        ).json()
        eur_group = client.post(
            "/api/v1/groups",
            json={"name": "EUR", "currency": "EUR", "member_ids": [bob.id]},
            headers=alice.headers,
        ).json()

        add_expense(client, alice, usd_group["id"], alice, alice, bob, amount="100.00")
        add_expense(client, bob, eur_group["id"], bob, alice, bob, amount="60.00")

        body = client.get("/api/v1/balances/me", headers=alice.headers).json()
        totals = {row["currency"]: row for row in body["totals"]}

        assert Decimal(totals["USD"]["net"]) == Decimal("50.00")
        assert Decimal(totals["EUR"]["net"]) == Decimal("-30.00")
        # Crucially, there is no combined figure that adds the two together.
        assert set(totals) == {"USD", "EUR"}

    def test_balance_with_one_person(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="40.00")

        body = client.get(f"/api/v1/balances/with/{bob.id}", headers=alice.headers).json()
        assert len(body) == 1
        assert Decimal(body[0]["amount"]) == Decimal("20.00")

    def test_settled_users_disappear_from_the_list(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="40.00")
        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="20.00")

        body = client.get("/api/v1/balances/me", headers=alice.headers).json()
        assert body["people"] == []
        assert body["totals"] == []
