from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor
from tests.test_settlements import add_expense, settle


class TestActivityFeed:
    def test_shows_expenses_and_settlements_together(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, description="Dinner")
        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00")

        page = client.get("/api/v1/activity", headers=alice.headers).json()

        assert page["total"] == 2
        assert {item["type"] for item in page["items"]} == {"expense", "settlement"}

    def test_summaries_read_naturally(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, description="Dinner")
        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00")

        # From Bob's point of view, Alice is a name and he is "You".
        summaries = [
            item["summary"]
            for item in client.get("/api/v1/activity", headers=bob.headers).json()["items"]
        ]
        assert "Alice Anderson added Dinner" in summaries
        assert "You paid Alice Anderson" in summaries

        # And from Alice's, the reverse.
        summaries = [
            item["summary"]
            for item in client.get("/api/v1/activity", headers=alice.headers).json()["items"]
        ]
        assert "You added Dinner" in summaries
        assert "Bob Brown paid you" in summaries

    def test_reports_the_effect_on_your_balance(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob, amount="60.00")

        alice_item = client.get("/api/v1/activity", headers=alice.headers).json()["items"][0]
        bob_item = client.get("/api/v1/activity", headers=bob.headers).json()["items"][0]

        assert Decimal(alice_item["your_impact"]) == Decimal("30.00")
        assert Decimal(bob_item["your_impact"]) == Decimal("-30.00")

    def test_newest_first_by_default_and_reversible(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        for name in ("First", "Second", "Third"):
            add_expense(client, alice, group["id"], alice, alice, bob, description=name)

        descending = client.get("/api/v1/activity", headers=alice.headers).json()["items"]
        ascending = client.get(
            "/api/v1/activity", params={"order": "asc"}, headers=alice.headers
        ).json()["items"]

        assert [i["summary"] for i in descending] == list(
            reversed([i["summary"] for i in ascending])
        )

    def test_filter_by_type(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob)
        settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="10.00")

        only_expenses = client.get(
            "/api/v1/activity", params={"type": "expense"}, headers=alice.headers
        ).json()
        assert only_expenses["total"] == 1
        assert only_expenses["items"][0]["type"] == "expense"

        only_settlements = client.get(
            "/api/v1/activity", params={"type": "settlement"}, headers=alice.headers
        ).json()
        assert only_settlements["total"] == 1

    def test_filter_by_group(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        first = make_group(alice, bob, name="One")
        second = make_group(alice, bob, name="Two")
        add_expense(client, alice, first["id"], alice, alice, bob, description="In one")
        add_expense(client, alice, second["id"], alice, alice, bob, description="In two")

        page = client.get(
            "/api/v1/activity", params={"group_id": first["id"]}, headers=alice.headers
        ).json()

        assert page["total"] == 1
        assert page["items"][0]["summary"] == "You added In one"
        assert page["items"][0]["group"]["name"] == "One"

    def test_filter_by_person(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        add_expense(client, alice, group["id"], alice, alice, bob, description="With Bob")
        add_expense(client, alice, group["id"], alice, alice, carol, description="With Carol")

        page = client.get(
            "/api/v1/activity", params={"with_user_id": carol.id}, headers=alice.headers
        ).json()

        assert page["total"] == 1
        assert page["items"][0]["summary"] == "You added With Carol"

    def test_pagination(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        for index in range(5):
            add_expense(client, alice, group["id"], alice, alice, bob, description=f"E{index}")

        first = client.get(
            "/api/v1/activity", params={"limit": 2, "offset": 0}, headers=alice.headers
        ).json()
        second = client.get(
            "/api/v1/activity", params={"limit": 2, "offset": 2}, headers=alice.headers
        ).json()

        assert first["total"] == second["total"] == 5
        assert len(first["items"]) == len(second["items"]) == 2
        assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})

    def test_an_outsider_sees_nothing(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        add_expense(client, alice, group["id"], alice, alice, bob)

        assert client.get("/api/v1/activity", headers=carol.headers).json()["total"] == 0

    def test_a_deleted_expense_leaves_the_feed(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        """The feed is derived, so it can never describe something that is gone."""
        group = make_group(alice, bob)
        expense = add_expense(client, alice, group["id"], alice, alice, bob)

        assert client.get("/api/v1/activity", headers=alice.headers).json()["total"] == 1

        client.delete(f"/api/v1/expenses/{expense['id']}", headers=alice.headers)

        assert client.get("/api/v1/activity", headers=alice.headers).json()["total"] == 0

    def test_a_non_member_cannot_filter_by_that_group(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice)
        response = client.get(
            "/api/v1/activity", params={"group_id": group["id"]}, headers=bob.headers
        )
        assert response.status_code == 404

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/api/v1/activity").status_code == 401
