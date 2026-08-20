"""The person page: three subsystems, one number.

The point of these tests is not that each figure can be computed — the group,
settlement and khata suites already prove that. It is that the *sum* is right and
that the person page never disagrees with the pages it aggregates.
"""

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor

TODAY = date.today().isoformat()


def summary(client: TestClient, viewer: Actor, person: Actor, **params) -> dict:
    response = client.get(
        f"/api/v1/people/{person.id}/summary", params=params, headers=viewer.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def open_khata(client: TestClient, owner: Actor, person: Actor, **extra) -> dict:
    response = client.post(
        "/api/v1/khata",
        json={"person_name": person.full_name, "person_user_id": person.id, "currency": "USD"}
        | extra,
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_entry(client: TestClient, owner: Actor, khata_id: str, kind: str, amount: str) -> None:
    response = client.post(
        f"/api/v1/khata/{khata_id}/entries",
        json={"entry_type": kind, "amount": amount, "entry_date": TODAY},
        headers=owner.headers,
    )
    assert response.status_code == 201, response.text


def add_expense(client: TestClient, payer: Actor, group_id: str, amount: str, *members) -> dict:
    response = client.post(
        "/api/v1/expenses",
        json={
            "group_id": group_id,
            "description": "Dinner",
            "amount": amount,
            "expense_date": TODAY,
            "split_type": "equal",
            "paid_by_id": payer.id,
            "splits": [{"user_id": member.id} for member in members],
        },
        headers=payer.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestTheUnifiedTotal:
    def test_group_and_khata_add_up(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        """A balance in each subsystem, and one total that is their sum."""
        befriend(alice, bob)
        group = make_group(alice, bob)

        # Alice pays 100 split equally: Bob owes her 50.
        add_expense(client, alice, group["id"], "100.00", alice, bob)

        # And she has given Bob 5000 on the khata, of which he returned 1000.
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "given", "5000.00")
        add_entry(client, alice, khata["id"], "received", "1000.00")

        body = summary(client, alice, bob)
        balances = body["balances"]

        assert Decimal(balances["group_balance"]) == Decimal("50.00")
        assert Decimal(balances["khata_balance"]) == Decimal("4000.00")
        assert Decimal(balances["loan_balance"]) == Decimal("0.00")
        assert Decimal(balances["total_balance"]) == Decimal("4050.00")

    def test_the_total_is_exactly_the_sum_of_its_parts(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        befriend(alice, bob)
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], "75.00", alice, bob)
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "given", "300.00")

        balances = summary(client, alice, bob)["balances"]
        parts = (
            Decimal(balances["group_balance"])
            + Decimal(balances["khata_balance"])
            + Decimal(balances["loan_balance"])
        )
        assert parts == Decimal(balances["total_balance"])

    def test_loans_are_reported_as_zero_not_omitted(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        """There is no loans feature yet, so the field must be a visible zero.

        Dropping it would make a partial breakdown look complete.
        """
        befriend(alice, bob)
        balances = summary(client, alice, bob)["balances"]
        assert "loan_balance" in balances
        assert Decimal(balances["loan_balance"]) == Decimal("0.00")

    def test_the_sign_convention_holds_across_subsystems(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        """Positive always means they owe you, whichever subsystem it came from."""
        befriend(alice, bob)
        group = make_group(alice, bob)

        # Bob pays, so Alice owes him: negative from her side.
        add_expense(client, bob, group["id"], "100.00", alice, bob)

        # And on the khata Bob has given Alice nothing, but Alice records receiving
        # 200 from Bob on her own book — which reduces what he owes her below zero.
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "received", "200.00")

        balances = summary(client, alice, bob)["balances"]
        assert Decimal(balances["group_balance"]) == Decimal("-50.00")
        assert Decimal(balances["khata_balance"]) == Decimal("-200.00")
        assert Decimal(balances["total_balance"]) == Decimal("-250.00")


class TestAgreementWithTheOtherPages:
    def test_the_group_figure_matches_the_balances_api(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        """If these two ever disagree, nobody trusts either."""
        befriend(alice, bob)
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], "90.00", alice, bob)

        person = summary(client, alice, bob)
        balances = client.get(
            f"/api/v1/balances/with/{bob.id}", headers=alice.headers
        ).json()

        owed = [entry for entry in balances if entry["currency"] == "USD"]
        assert owed, balances
        assert Decimal(owed[0]["amount"]) == Decimal(person["balances"]["group_balance"])

    def test_the_khata_figure_matches_the_khata_api(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "given", "1234.56")

        person = summary(client, alice, bob)
        detail = client.get(f"/api/v1/khata/{khata['id']}", headers=alice.headers).json()

        assert Decimal(detail["balance"]) == Decimal(person["balances"]["khata_balance"])
        assert person["khata_ids"] == [khata["id"]]

    def test_a_settlement_moves_the_group_figure_and_is_reported_separately(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        befriend(alice, bob)
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], "100.00", alice, bob)

        paid = client.post(
            "/api/v1/settlements",
            json={
                "from_user_id": bob.id,
                "to_user_id": alice.id,
                "amount": "20.00",
                "currency": "USD",
                "settled_on": TODAY,
                "group_id": group["id"],
            },
            headers=bob.headers,
        )
        assert paid.status_code == 201, paid.text

        balances = summary(client, alice, bob)["balances"]
        assert Decimal(balances["group_balance"]) == Decimal("30.00")
        # Reported for context, but not double-counted into the total.
        assert Decimal(balances["settled_total"]) == Decimal("20.00")
        assert Decimal(balances["total_balance"]) == Decimal("30.00")


class TestCurrency:
    def test_a_khata_in_another_currency_is_not_added_in(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        """Adding PKR to USD would be arithmetic on incompatible units."""
        befriend(alice, bob)
        khata = open_khata(client, alice, bob, currency="PKR")
        add_entry(client, alice, khata["id"], "given", "5000.00")

        usd = summary(client, alice, bob)
        assert Decimal(usd["balances"]["khata_balance"]) == Decimal("0.00")

        pkr = summary(client, alice, bob, currency="PKR")
        assert Decimal(pkr["balances"]["khata_balance"]) == Decimal("5000.00")
        assert pkr["currency"] == "PKR"


class TestActivity:
    def test_the_feed_merges_all_three_sources(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        befriend(alice, bob)
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], "100.00", alice, bob)
        client.post(
            "/api/v1/settlements",
            json={
                "from_user_id": bob.id,
                "to_user_id": alice.id,
                "amount": "10.00",
                "currency": "USD",
                "settled_on": TODAY,
            },
            headers=bob.headers,
        )
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "given", "500.00")

        response = client.get(f"/api/v1/people/{bob.id}/activity", headers=alice.headers)
        assert response.status_code == 200, response.text
        body = response.json()

        kinds = {item["kind"] for item in body["items"]}
        assert kinds == {"expense", "settlement", "khata_entry"}
        assert body["total"] == 3

    def test_a_khata_entry_carries_its_khata_id(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "given", "500.00")

        body = client.get(
            f"/api/v1/people/{bob.id}/activity", headers=alice.headers
        ).json()
        entry = body["items"][0]
        assert entry["kind"] == "khata_entry"
        assert entry["khata_id"] == khata["id"]
        assert Decimal(entry["your_impact"]) == Decimal("500.00")

    def test_activity_pages(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        khata = open_khata(client, alice, bob)
        for _ in range(5):
            add_entry(client, alice, khata["id"], "given", "100.00")

        first = client.get(
            f"/api/v1/people/{bob.id}/activity",
            params={"limit": 2},
            headers=alice.headers,
        ).json()
        second = client.get(
            f"/api/v1/people/{bob.id}/activity",
            params={"limit": 2, "offset": 2},
            headers=alice.headers,
        ).json()

        assert first["total"] == 5
        assert len(first["items"]) == 2
        ids = {item["id"] for item in first["items"]} | {item["id"] for item in second["items"]}
        assert len(ids) == 4

    def test_the_summary_carries_recent_activity(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "given", "500.00")

        body = summary(client, alice, bob)
        assert len(body["recent_activity"]) == 1
        assert body["recent_activity"][0]["kind"] == "khata_entry"


class TestTheList:
    def test_khata_only_contacts_appear_without_a_user(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Someone with no account is still someone you are owed money by.

        They cannot have a person page, so the row points at their khata and says so.
        """
        khata = client.post(
            "/api/v1/khata",
            json={"person_name": "Usman", "currency": "USD"},
            headers=alice.headers,
        ).json()
        add_entry(client, alice, khata["id"], "given", "800.00")

        body = client.get("/api/v1/people", headers=alice.headers).json()
        usman = next(item for item in body["items"] if item["name"] == "Usman")

        assert usman["has_account"] is False
        assert usman["user"] is None
        assert usman["khata_id"] == khata["id"]
        assert Decimal(usman["total_balance"]) == Decimal("800.00")

    def test_people_you_share_a_group_with_appear(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        befriend(alice, bob)
        make_group(alice, bob)

        body = client.get("/api/v1/people", headers=alice.headers).json()
        names = {item["name"] for item in body["items"]}
        assert bob.full_name in names
        assert alice.full_name not in names  # You are not on your own people list.

    def test_search_narrows_the_list(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        client.post(
            "/api/v1/khata", json={"person_name": "Zubair"}, headers=alice.headers
        )

        body = client.get(
            "/api/v1/people", params={"search": "zubair"}, headers=alice.headers
        ).json()
        assert [item["name"] for item in body["items"]] == ["Zubair"]


class TestPrivacy:
    def test_you_only_ever_see_your_own_side(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        """Alice's khata about Bob is hers alone — Bob's page shows him nothing."""
        befriend(alice, bob)
        khata = open_khata(client, alice, bob)
        add_entry(client, alice, khata["id"], "given", "5000.00")

        hers = summary(client, alice, bob)
        his = summary(client, bob, alice)

        assert Decimal(hers["balances"]["khata_balance"]) == Decimal("5000.00")
        assert Decimal(his["balances"]["khata_balance"]) == Decimal("0.00")

    def test_an_unknown_person_is_a_404(self, client: TestClient, alice: Actor) -> None:
        import uuid

        response = client.get(
            f"/api/v1/people/{uuid.uuid4()}/summary", headers=alice.headers
        )
        assert response.status_code == 404

    def test_the_endpoints_need_a_token(self, client: TestClient, alice: Actor) -> None:
        assert client.get(f"/api/v1/people/{alice.id}/summary").status_code == 401
        assert client.get("/api/v1/people").status_code == 401
