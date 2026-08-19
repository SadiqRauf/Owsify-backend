from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor

TODAY = "2026-08-01"


def expense_body(group_id: str | None, payer: Actor, *participants: Actor, **overrides) -> dict:
    body = {
        "description": "Dinner",
        "amount": "60.00",
        "expense_date": TODAY,
        "paid_by_id": payer.id,
        "split_type": "equal",
        "splits": [{"user_id": actor.id} for actor in participants],
    }
    if group_id is not None:
        body["group_id"] = group_id
    return body | overrides


def total_of(expense: dict) -> Decimal:
    return sum((Decimal(split["amount"]) for split in expense["splits"]), Decimal("0.00"))


class TestCreate:
    def test_equal_split_across_three(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, carol),
            headers=alice.headers,
        )

        assert response.status_code == 201
        body = response.json()
        assert len(body["splits"]) == 3
        assert total_of(body) == Decimal("60.00")
        assert all(Decimal(split["amount"]) == Decimal("20.00") for split in body["splits"])

    def test_indivisible_equal_split_still_sums_exactly(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, carol, amount="100.00"),
            headers=alice.headers,
        )

        body = response.json()
        assert total_of(body) == Decimal("100.00")
        amounts = sorted(Decimal(split["amount"]) for split in body["splits"])
        assert amounts == [Decimal("33.33"), Decimal("33.33"), Decimal("33.34")]

    def test_exact_split(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(
                group["id"],
                alice,
                alice,
                bob,
                amount="50.00",
                split_type="exact",
                splits=[
                    {"user_id": alice.id, "value": "20.00"},
                    {"user_id": bob.id, "value": "30.00"},
                ],
            ),
            headers=alice.headers,
        )

        assert response.status_code == 201
        shares = {s["user"]["id"]: Decimal(s["amount"]) for s in response.json()["splits"]}
        assert shares[alice.id] == Decimal("20.00")
        assert shares[bob.id] == Decimal("30.00")

    def test_percentage_split_records_the_percentages(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(
                group["id"],
                alice,
                alice,
                bob,
                amount="100.00",
                split_type="percentage",
                splits=[
                    {"user_id": alice.id, "value": "70"},
                    {"user_id": bob.id, "value": "30"},
                ],
            ),
            headers=alice.headers,
        )

        assert response.status_code == 201
        shares = {s["user"]["id"]: s for s in response.json()["splits"]}
        assert Decimal(shares[alice.id]["amount"]) == Decimal("70.00")
        assert Decimal(shares[alice.id]["percentage"]) == Decimal("70")
        assert total_of(response.json()) == Decimal("100.00")

    def test_percentage_rounding_still_sums_exactly(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(
                group["id"],
                alice,
                alice,
                bob,
                carol,
                amount="10.00",
                split_type="percentage",
                splits=[
                    {"user_id": alice.id, "value": "33.33"},
                    {"user_id": bob.id, "value": "33.33"},
                    {"user_id": carol.id, "value": "33.34"},
                ],
            ),
            headers=alice.headers,
        )

        assert response.status_code == 201
        assert total_of(response.json()) == Decimal("10.00")

    def test_annotates_the_callers_position(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, amount="50.00"),
            headers=alice.headers,
        ).json()

        # Alice paid 50 and owes 25, so she is up 25.
        assert Decimal(created["my_share"]) == Decimal("25.00")
        assert Decimal(created["my_net"]) == Decimal("25.00")

        from_bob = client.get(f"/api/v1/expenses/{created['id']}", headers=bob.headers).json()
        assert Decimal(from_bob["my_share"]) == Decimal("25.00")
        assert Decimal(from_bob["my_net"]) == Decimal("-25.00")

    def test_a_personal_expense_between_friends(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(None, alice, alice, bob, amount="20.00"),
            headers=alice.headers,
        )

        assert response.status_code == 201
        assert response.json()["group_id"] is None

    def test_takes_the_groups_currency(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        group = client.post(
            "/api/v1/groups", json={"name": "EU", "currency": "EUR"}, headers=alice.headers
        ).json()
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, currency="USD"),
            headers=alice.headers,
        )
        assert response.json()["currency"] == "EUR"


class TestCreateValidation:
    def test_exact_split_that_does_not_add_up(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(
                group["id"],
                alice,
                alice,
                bob,
                amount="50.00",
                split_type="exact",
                splits=[
                    {"user_id": alice.id, "value": "20.00"},
                    {"user_id": bob.id, "value": "20.00"},
                ],
            ),
            headers=alice.headers,
        )

        assert response.status_code == 422
        assert "40.00" in response.json()["error"]["details"][0]["message"]

    def test_percentages_that_do_not_reach_100(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(
                group["id"],
                alice,
                alice,
                bob,
                split_type="percentage",
                splits=[
                    {"user_id": alice.id, "value": "50"},
                    {"user_id": bob.id, "value": "40"},
                ],
            ),
            headers=alice.headers,
        )

        assert response.status_code == 422
        assert "90%" in response.json()["error"]["details"][0]["message"]

    def test_exact_split_missing_a_value(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(
                group["id"],
                alice,
                alice,
                bob,
                split_type="exact",
                splits=[{"user_id": alice.id, "value": "60.00"}, {"user_id": bob.id}],
            ),
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_a_non_member_cannot_be_in_a_group_split(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, carol),
            headers=alice.headers,
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "not_a_member"

    def test_a_stranger_cannot_be_in_a_personal_split(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(None, alice, alice, bob),
            headers=alice.headers,
        )

        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "not_a_friend"

    def test_a_non_member_cannot_post_to_a_group(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], bob, bob),
            headers=bob.headers,
        )
        assert response.status_code == 404

    def test_rejects_zero_and_negative_amounts(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        group = make_group(alice)
        for amount in ("0.00", "-5.00"):
            response = client.post(
                "/api/v1/expenses",
                json=expense_body(group["id"], alice, alice, amount=amount),
                headers=alice.headers,
            )
            assert response.status_code == 422, amount

    def test_rejects_a_future_date(self, client: TestClient, alice: Actor, make_group) -> None:
        group = make_group(alice)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, expense_date="2099-01-01"),
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_rejects_a_blank_description(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        group = make_group(alice)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, description="   "),
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_rejects_a_duplicate_participant(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(
                group["id"], alice, alice, splits=[{"user_id": bob.id}, {"user_id": bob.id}]
            ),
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_rejects_an_empty_split_list(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        group = make_group(alice)
        response = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, splits=[]),
            headers=alice.headers,
        )
        assert response.status_code == 422


class TestReadAndList:
    def test_group_history_and_paging(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        for index in range(3):
            client.post(
                "/api/v1/expenses",
                json=expense_body(group["id"], alice, alice, bob, description=f"Item {index}"),
                headers=alice.headers,
            )

        page = client.get(
            f"/api/v1/groups/{group['id']}/expenses",
            params={"limit": 2, "offset": 0},
            headers=bob.headers,
        ).json()

        assert page["total"] == 3
        assert len(page["items"]) == 2

    def test_listing_is_scoped_to_what_you_can_see(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob),
            headers=alice.headers,
        )

        assert client.get("/api/v1/expenses", headers=carol.headers).json()["total"] == 0
        assert client.get("/api/v1/expenses", headers=bob.headers).json()["total"] == 1

    def test_an_outsider_cannot_read_an_expense(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob),
            headers=alice.headers,
        ).json()

        assert (
            client.get(f"/api/v1/expenses/{created['id']}", headers=carol.headers).status_code
            == 404
        )

    def test_filter_by_group(
        self, client: TestClient, alice: Actor, make_group
    ) -> None:
        first = make_group(alice, name="One")
        second = make_group(alice, name="Two")
        client.post(
            "/api/v1/expenses", json=expense_body(first["id"], alice, alice), headers=alice.headers
        )
        client.post(
            "/api/v1/expenses", json=expense_body(second["id"], alice, alice), headers=alice.headers
        )

        page = client.get(
            "/api/v1/expenses", params={"group_id": first["id"]}, headers=alice.headers
        ).json()
        assert page["total"] == 1


class TestUpdate:
    def test_editing_the_amount_recomputes_an_equal_split(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, amount="60.00"),
            headers=alice.headers,
        ).json()

        updated = client.patch(
            f"/api/v1/expenses/{created['id']}", json={"amount": "80.00"}, headers=alice.headers
        )

        assert updated.status_code == 200
        assert total_of(updated.json()) == Decimal("80.00")
        assert all(Decimal(s["amount"]) == Decimal("40.00") for s in updated.json()["splits"])

    def test_changing_the_participants(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, amount="60.00"),
            headers=alice.headers,
        ).json()

        updated = client.patch(
            f"/api/v1/expenses/{created['id']}",
            json={
                "split_type": "equal",
                "splits": [{"user_id": alice.id}, {"user_id": bob.id}, {"user_id": carol.id}],
            },
            headers=alice.headers,
        )

        assert updated.status_code == 200
        assert len(updated.json()["splits"]) == 3
        assert total_of(updated.json()) == Decimal("60.00")

    def test_switching_to_a_percentage_split(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, amount="100.00"),
            headers=alice.headers,
        ).json()

        updated = client.patch(
            f"/api/v1/expenses/{created['id']}",
            json={
                "split_type": "percentage",
                "splits": [
                    {"user_id": alice.id, "value": "25"},
                    {"user_id": bob.id, "value": "75"},
                ],
            },
            headers=alice.headers,
        )

        shares = {s["user"]["id"]: Decimal(s["amount"]) for s in updated.json()["splits"]}
        assert shares[bob.id] == Decimal("75.00")

    def test_splits_require_a_split_type(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob),
            headers=alice.headers,
        ).json()

        response = client.patch(
            f"/api/v1/expenses/{created['id']}",
            json={"splits": [{"user_id": alice.id}]},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_a_bystander_cannot_edit(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, carol),
            headers=alice.headers,
        ).json()

        response = client.patch(
            f"/api/v1/expenses/{created['id']}",
            json={"description": "Hijacked"},
            headers=carol.headers,
        )
        assert response.status_code == 403

    def test_the_payer_can_edit_even_if_someone_else_added_it(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], bob, alice, bob),
            headers=alice.headers,
        ).json()

        response = client.patch(
            f"/api/v1/expenses/{created['id']}", json={"description": "Fixed"}, headers=bob.headers
        )
        assert response.status_code == 200

    def test_a_group_admin_can_edit(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        client.patch(
            f"/api/v1/groups/{group['id']}/members/{carol.id}",
            json={"role": "admin"},
            headers=alice.headers,
        )
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob),
            headers=alice.headers,
        ).json()

        response = client.patch(
            f"/api/v1/expenses/{created['id']}",
            json={"description": "Moderated"},
            headers=carol.headers,
        )
        assert response.status_code == 200


class TestDelete:
    def test_creator_can_delete(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob),
            headers=alice.headers,
        ).json()

        assert (
            client.delete(f"/api/v1/expenses/{created['id']}", headers=alice.headers).status_code
            == 200
        )
        assert (
            client.get(f"/api/v1/expenses/{created['id']}", headers=alice.headers).status_code
            == 404
        )

    def test_a_bystander_cannot_delete(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, carol),
            headers=alice.headers,
        ).json()

        response = client.delete(f"/api/v1/expenses/{created['id']}", headers=carol.headers)
        assert response.status_code == 403

    def test_deleting_a_group_removes_its_expenses(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        created = client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob),
            headers=alice.headers,
        ).json()

        client.delete(f"/api/v1/groups/{group['id']}", headers=alice.headers)
        assert (
            client.get(f"/api/v1/expenses/{created['id']}", headers=alice.headers).status_code
            == 404
        )


class TestBalances:
    """Ported to /balances/me, which reports per currency rather than one figure."""

    def test_reflects_who_paid(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, amount="60.00"),
            headers=alice.headers,
        )

        alice_view = client.get("/api/v1/balances/me", headers=alice.headers).json()
        usd = next(row for row in alice_view["totals"] if row["currency"] == "USD")
        assert Decimal(usd["owed_to_you"]) == Decimal("30.00")
        assert Decimal(usd["net"]) == Decimal("30.00")
        assert alice_view["people"][0]["user"]["id"] == bob.id

        bob_view = client.get("/api/v1/balances/me", headers=bob.headers).json()
        usd = next(row for row in bob_view["totals"] if row["currency"] == "USD")
        assert Decimal(usd["you_owe"]) == Decimal("30.00")
        assert Decimal(usd["net"]) == Decimal("-30.00")

    def test_opposite_expenses_cancel_out(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], alice, alice, bob, amount="60.00"),
            headers=alice.headers,
        )
        client.post(
            "/api/v1/expenses",
            json=expense_body(group["id"], bob, alice, bob, amount="60.00"),
            headers=bob.headers,
        )

        view = client.get("/api/v1/balances/me", headers=alice.headers).json()
        assert view["totals"] == []
        assert view["people"] == []

    def test_can_be_scoped_to_one_group(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        first = make_group(alice, bob, name="One")
        second = make_group(alice, bob, name="Two")
        client.post(
            "/api/v1/expenses",
            json=expense_body(first["id"], alice, alice, bob, amount="60.00"),
            headers=alice.headers,
        )
        client.post(
            "/api/v1/expenses",
            json=expense_body(second["id"], alice, alice, bob, amount="20.00"),
            headers=alice.headers,
        )

        scoped = client.get(
            f"/api/v1/balances/groups/{second['id']}", headers=alice.headers
        ).json()
        assert Decimal(scoped["your_net"]) == Decimal("10.00")

        overall = client.get("/api/v1/balances/me", headers=alice.headers).json()
        usd = next(row for row in overall["totals"] if row["currency"] == "USD")
        assert Decimal(usd["net"]) == Decimal("40.00")

    def test_starts_empty(self, client: TestClient, alice: Actor) -> None:
        view = client.get("/api/v1/balances/me", headers=alice.headers).json()
        assert view["totals"] == []
        assert view["people"] == []
