from fastapi.testclient import TestClient

from tests.conftest import Actor


class TestCreateAndRead:
    def test_creator_becomes_owner(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/groups",
            json={"name": "  Ski trip  ", "description": "Chalet", "currency": "eur"},
            headers=alice.headers,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "Ski trip"
        assert body["currency"] == "EUR"
        assert body["my_role"] == "owner"
        assert body["member_count"] == 1
        assert body["members"][0]["user"]["id"] == alice.id

    def test_creates_with_initial_members(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor
    ) -> None:
        response = client.post(
            "/api/v1/groups",
            json={"name": "Flat", "member_ids": [bob.id, carol.id]},
            headers=alice.headers,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["member_count"] == 3
        roles = {member["user"]["id"]: member["role"] for member in body["members"]}
        assert roles[alice.id] == "owner"
        assert roles[bob.id] == "member"

    def test_lists_only_your_groups(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        make_group(alice, name="Alice only")
        make_group(bob, name="Bob only")

        names = [
            group["name"] for group in client.get("/api/v1/groups", headers=alice.headers).json()
        ]
        assert names == ["Alice only"]

    def test_a_non_member_gets_404_not_403(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice)
        # 404 rather than 403 so group ids cannot be probed for existence.
        response = client.get(f"/api/v1/groups/{group['id']}", headers=bob.headers)
        assert response.status_code == 404

    def test_rejects_a_blank_name(self, client: TestClient, alice: Actor) -> None:
        response = client.post("/api/v1/groups", json={"name": "   "}, headers=alice.headers)
        assert response.status_code == 422

    def test_rejects_an_unknown_member(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/groups",
            json={"name": "X", "member_ids": ["00000000-0000-0000-0000-000000000000"]},
            headers=alice.headers,
        )
        assert response.status_code == 404


class TestUpdateAndDelete:
    def test_owner_can_edit(self, client: TestClient, alice: Actor, make_group) -> None:
        group = make_group(alice)
        response = client.patch(
            f"/api/v1/groups/{group['id']}",
            json={"name": "Renamed", "emoji": "🏔"},
            headers=alice.headers,
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"
        assert response.json()["emoji"] == "🏔"

    def test_a_plain_member_cannot_edit(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.patch(
            f"/api/v1/groups/{group['id']}", json={"name": "Nope"}, headers=bob.headers
        )
        assert response.status_code == 403

    def test_an_admin_can_edit(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        client.patch(
            f"/api/v1/groups/{group['id']}/members/{bob.id}",
            json={"role": "admin"},
            headers=alice.headers,
        )

        response = client.patch(
            f"/api/v1/groups/{group['id']}", json={"name": "Admin edit"}, headers=bob.headers
        )
        assert response.status_code == 200

    def test_only_the_owner_can_delete(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        client.patch(
            f"/api/v1/groups/{group['id']}/members/{bob.id}",
            json={"role": "admin"},
            headers=alice.headers,
        )

        assert (
            client.delete(f"/api/v1/groups/{group['id']}", headers=bob.headers).status_code == 403
        )
        assert (
            client.delete(f"/api/v1/groups/{group['id']}", headers=alice.headers).status_code == 200
        )
        assert client.get(f"/api/v1/groups/{group['id']}", headers=alice.headers).status_code == 404


class TestMembership:
    def test_add_by_id_and_email(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice)
        response = client.post(
            f"/api/v1/groups/{group['id']}/members",
            json={"user_ids": [bob.id], "emails": [carol.email]},
            headers=alice.headers,
        )

        assert response.status_code == 201
        assert response.json()["member_count"] == 3

    def test_adding_an_existing_member_is_a_conflict(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            f"/api/v1/groups/{group['id']}/members",
            json={"user_ids": [bob.id]},
            headers=alice.headers,
        )
        assert response.status_code == 409

    def test_a_plain_member_cannot_add(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.post(
            f"/api/v1/groups/{group['id']}/members",
            json={"user_ids": [carol.id]},
            headers=bob.headers,
        )
        assert response.status_code == 403

    def test_owner_removes_a_member(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.delete(
            f"/api/v1/groups/{group['id']}/members/{bob.id}", headers=alice.headers
        )

        assert response.status_code == 200
        detail = client.get(f"/api/v1/groups/{group['id']}", headers=alice.headers).json()
        assert detail["member_count"] == 1

    def test_a_member_can_leave(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.delete(
            f"/api/v1/groups/{group['id']}/members/{bob.id}", headers=bob.headers
        )

        assert response.status_code == 200
        assert client.get(f"/api/v1/groups/{group['id']}", headers=bob.headers).status_code == 404

    def test_a_member_cannot_remove_another(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        response = client.delete(
            f"/api/v1/groups/{group['id']}/members/{carol.id}", headers=bob.headers
        )
        assert response.status_code == 403

    def test_the_owner_cannot_leave(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.delete(
            f"/api/v1/groups/{group['id']}/members/{alice.id}", headers=alice.headers
        )

        assert response.status_code == 400
        assert "ownership" in response.json()["error"]["message"].lower()

    def test_a_member_with_expenses_cannot_be_removed(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        client.post(
            "/api/v1/expenses",
            json={
                "group_id": group["id"],
                "description": "Dinner",
                "amount": "40.00",
                "expense_date": "2026-08-01",
                "paid_by_id": alice.id,
                "split_type": "equal",
                "splits": [{"user_id": alice.id}, {"user_id": bob.id}],
            },
            headers=alice.headers,
        )

        response = client.delete(
            f"/api/v1/groups/{group['id']}/members/{bob.id}", headers=alice.headers
        )
        assert response.status_code == 409

    def test_role_change_and_ownership_transfer(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)

        promoted = client.patch(
            f"/api/v1/groups/{group['id']}/members/{bob.id}",
            json={"role": "admin"},
            headers=alice.headers,
        )
        assert promoted.status_code == 200

        transferred = client.post(
            f"/api/v1/groups/{group['id']}/transfer-ownership/{bob.id}", headers=alice.headers
        )
        assert transferred.status_code == 200

        roles = {member["user"]["id"]: member["role"] for member in transferred.json()["members"]}
        assert roles[bob.id] == "owner"
        assert roles[alice.id] == "admin"

    def test_cannot_assign_the_owner_role_directly(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        response = client.patch(
            f"/api/v1/groups/{group['id']}/members/{bob.id}",
            json={"role": "owner"},
            headers=alice.headers,
        )
        assert response.status_code == 422
