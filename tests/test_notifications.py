from fastapi.testclient import TestClient

from tests.conftest import Actor
from tests.test_settlements import add_expense, settle


def inbox(client: TestClient, actor: Actor, **params) -> list[dict]:
    response = client.get("/api/v1/notifications", params=params, headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def types_for(client: TestClient, actor: Actor) -> list[str]:
    return [item["type"] for item in inbox(client, actor)]


def clear(client: TestClient, *actors: Actor) -> None:
    """Mark everything read, so a test can assert on only what it caused."""
    for actor in actors:
        client.post("/api/v1/notifications/read-all", headers=actor.headers)


def unread(client: TestClient, actor: Actor) -> list[dict]:
    return inbox(client, actor, unread_only=True)


class TestExpenses:
    def test_everyone_on_an_expense_hears_about_it_except_whoever_added_it(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        clear(client, alice, bob, carol)

        add_expense(client, alice, group["id"], alice, alice, bob, carol, amount="90.00")

        assert unread(client, alice) == []
        for member in (bob, carol):
            [notification] = unread(client, member)
            assert notification["type"] == "expense_added"
            assert notification["actor"]["id"] == alice.id
            assert notification["message"] == (
                'Alice Anderson added "Dinner" (USD 90.00) in Trip. Your share is USD 30.00.'
            )
            assert notification["href"].startswith("/expenses/")

    def test_a_group_member_not_on_the_expense_is_not_told(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        clear(client, carol)

        add_expense(client, alice, group["id"], alice, alice, bob)

        assert unread(client, carol) == []

    def test_an_edit_only_notifies_people_whose_share_moved(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        expense = add_expense(client, alice, group["id"], alice, alice, bob, carol, amount="90.00")
        clear(client, alice, bob, carol)

        renamed = client.patch(
            f"/api/v1/expenses/{expense['id']}",
            json={"description": "Supper"},
            headers=alice.headers,
        )
        assert renamed.status_code == 200
        assert unread(client, bob) == []

        # Carol drops off; Bob's share goes from 30 to 45.
        resplit = client.patch(
            f"/api/v1/expenses/{expense['id']}",
            json={"split_type": "equal", "splits": [{"user_id": alice.id}, {"user_id": bob.id}]},
            headers=alice.headers,
        )
        assert resplit.status_code == 200, resplit.text

        [for_bob] = unread(client, bob)
        assert for_bob["type"] == "expense_updated"
        assert for_bob["message"].endswith("Your share is now USD 45.00.")
        [for_carol] = unread(client, carol)
        assert for_carol["message"].endswith("You are no longer part of it.")

    def test_a_deleted_expense_is_still_described_by_name(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        expense = add_expense(client, alice, group["id"], alice, alice, bob, amount="60.00")
        clear(client, bob)

        assert client.delete(f"/api/v1/expenses/{expense['id']}", headers=alice.headers).status_code == 200

        [notification] = unread(client, bob)
        assert notification["type"] == "expense_deleted"
        assert notification["message"] == 'Alice Anderson deleted "Dinner" (USD 60.00) in Trip.'
        assert notification["href"] == f"/groups/{group['id']}"


class TestSettlements:
    def test_the_payee_is_told_they_were_paid(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        clear(client, alice, bob)

        assert settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00").status_code == 201

        assert unread(client, bob) == []
        [notification] = unread(client, alice)
        assert notification["type"] == "settlement_recorded"
        assert notification["message"] == "Bob Brown paid you USD 30.00 in Trip."

    def test_a_third_party_recording_it_notifies_both_sides(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        clear(client, alice, bob, carol)

        settle(client, carol, group_id=group["id"], payer=bob, payee=alice, amount="10.00")

        assert unread(client, carol) == []
        [for_alice] = unread(client, alice)
        assert for_alice["message"] == "Carol Clark recorded that Bob Brown paid you USD 10.00 in Trip."
        [for_bob] = unread(client, bob)
        assert for_bob["message"] == "Carol Clark recorded that you paid Alice Anderson USD 10.00 in Trip."

    def test_changing_the_amount_or_deleting_notifies_the_other_side(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob)
        settlement = settle(client, bob, group_id=group["id"], payer=bob, payee=alice, amount="30.00").json()
        clear(client, alice)

        client.patch(f"/api/v1/settlements/{settlement['id']}", json={"notes": "cash"}, headers=bob.headers)
        assert unread(client, alice) == []

        client.patch(f"/api/v1/settlements/{settlement['id']}", json={"amount": "25.00"}, headers=bob.headers)
        client.delete(f"/api/v1/settlements/{settlement['id']}", headers=bob.headers)

        assert [n["type"] for n in unread(client, alice)] == ["settlement_deleted", "settlement_updated"]


class TestFriends:
    def test_request_and_acceptance(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        sent = client.post("/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers)

        [request] = unread(client, bob)
        assert request["type"] == "friend_request"
        assert request["message"] == "Alice Anderson sent you a friend request."

        client.post(f"/api/v1/friends/requests/{sent.json()['id']}/accept", headers=bob.headers)

        [accepted] = unread(client, alice)
        assert accepted["type"] == "friend_accepted"

    def test_asking_someone_who_already_asked_you_accepts_and_says_so(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        client.post("/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers)
        client.post("/api/v1/friends/requests", json={"user_id": alice.id}, headers=bob.headers)

        assert types_for(client, alice) == ["friend_accepted"]

    def test_a_rejection_is_silent(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        sent = client.post("/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers)
        client.post(f"/api/v1/friends/requests/{sent.json()['id']}/reject", headers=bob.headers)

        assert inbox(client, alice) == []

    def test_an_invitee_signing_up_tells_the_inviter(self, client: TestClient, alice: Actor) -> None:
        client.post("/api/v1/friends/invitations", json={"email": "new@example.com"}, headers=alice.headers)
        client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "full_name": "Nina New", "password": "hunter2pass"},
        )

        [notification] = inbox(client, alice)
        assert notification["type"] == "invitation_accepted"
        assert notification["message"].startswith("Nina New joined")


class TestGroups:
    def test_members_added_at_creation_are_told_but_the_creator_is_not(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, name="Flat")

        assert inbox(client, alice) == []
        [notification] = inbox(client, bob)
        assert notification["message"] == "Alice Anderson added you to Flat."
        assert notification["href"] == f"/groups/{group['id']}"

    def test_adding_a_member_later(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice)
        client.post(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [bob.id]}, headers=alice.headers)

        assert types_for(client, bob) == ["group_added"]

    def test_being_removed_is_news_but_leaving_is_not(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, carol)
        clear(client, alice, bob, carol)

        client.delete(f"/api/v1/groups/{group['id']}/members/{bob.id}", headers=alice.headers)
        client.delete(f"/api/v1/groups/{group['id']}/members/{carol.id}", headers=carol.headers)

        assert [n["type"] for n in unread(client, bob)] == ["group_removed"]
        assert unread(client, carol) == []

    def test_role_changes_and_ownership(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, name="Flat")
        clear(client, bob)

        client.patch(f"/api/v1/groups/{group['id']}/members/{bob.id}", json={"role": "admin"}, headers=alice.headers)
        # Setting the role it already has changes nothing, so says nothing.
        client.patch(f"/api/v1/groups/{group['id']}/members/{bob.id}", json={"role": "admin"}, headers=alice.headers)
        client.post(f"/api/v1/groups/{group['id']}/transfer-ownership/{bob.id}", headers=alice.headers)

        assert [n["message"] for n in unread(client, bob)] == [
            "Alice Anderson made you the owner of Flat.",
            "Alice Anderson made you an admin of Flat.",
        ]

    def test_deleting_a_group_tells_every_other_member(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        group = make_group(alice, bob, name="Flat")
        clear(client, alice, bob)

        client.delete(f"/api/v1/groups/{group['id']}", headers=alice.headers)

        assert unread(client, alice) == []
        [notification] = unread(client, bob)
        assert notification["message"] == "Alice Anderson deleted the group Flat."
        assert notification["href"] == "/groups"


class TestEndpoints:
    def test_unread_count_and_marking_read(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        make_group(alice, bob, name="One")
        make_group(alice, bob, name="Two")

        count = client.get("/api/v1/notifications/unread-count", headers=bob.headers).json()
        assert count == {"count": 2}

        newest = inbox(client, bob)[0]
        marked = client.post(f"/api/v1/notifications/{newest['id']}/read", headers=bob.headers)
        assert marked.status_code == 200
        assert marked.json()["is_read"] is True

        page = client.get("/api/v1/notifications", headers=bob.headers).json()
        assert page["total"] == 2
        assert page["unread_count"] == 1

        response = client.post("/api/v1/notifications/read-all", headers=bob.headers)
        assert response.json()["message"] == "Marked 1 notification as read."
        assert client.get("/api/v1/notifications/unread-count", headers=bob.headers).json() == {"count": 0}

    def test_someone_elses_notification_is_a_404(
        self, client: TestClient, alice: Actor, bob: Actor, make_group
    ) -> None:
        make_group(alice, bob)
        [notification] = inbox(client, bob)

        response = client.post(f"/api/v1/notifications/{notification['id']}/read", headers=alice.headers)
        assert response.status_code == 404

    def test_needs_a_signed_in_user(self, client: TestClient) -> None:
        assert client.get("/api/v1/notifications").status_code == 401
