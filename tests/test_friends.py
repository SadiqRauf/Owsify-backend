from fastapi.testclient import TestClient

from tests.conftest import Actor


class TestFriendRequests:
    def test_send_and_accept(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        sent = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        assert sent.status_code == 201
        assert sent.json()["status"] == "pending"
        assert sent.json()["user"]["id"] == bob.id
        assert sent.json()["is_incoming"] is False

        incoming = client.get("/api/v1/friends/requests", headers=bob.headers).json()
        assert len(incoming) == 1
        assert incoming[0]["is_incoming"] is True
        assert incoming[0]["user"]["id"] == alice.id

        accepted = client.post(
            f"/api/v1/friends/requests/{sent.json()['id']}/accept", headers=bob.headers
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "accepted"

        for actor, other in ((alice, bob), (bob, alice)):
            friends = client.get("/api/v1/friends", headers=actor.headers).json()
            assert [friend["user"]["id"] for friend in friends] == [other.id]

    def test_send_by_email(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        response = client.post(
            "/api/v1/friends/requests", json={"email": bob.email}, headers=alice.headers
        )
        assert response.status_code == 201
        assert response.json()["user"]["id"] == bob.id

    def test_reject(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        sent = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        rejected = client.post(
            f"/api/v1/friends/requests/{sent.json()['id']}/reject", headers=bob.headers
        )

        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert client.get("/api/v1/friends", headers=alice.headers).json() == []

    def test_a_rejected_request_can_be_sent_again(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        sent = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        client.post(f"/api/v1/friends/requests/{sent.json()['id']}/reject", headers=bob.headers)

        again = client.post(
            "/api/v1/friends/requests", json={"user_id": alice.id}, headers=bob.headers
        )
        assert again.status_code == 201
        assert again.json()["status"] == "pending"

    def test_requesting_someone_who_already_asked_accepts_instead(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        client.post("/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers)

        response = client.post(
            "/api/v1/friends/requests", json={"user_id": alice.id}, headers=bob.headers
        )
        assert response.status_code == 201
        assert response.json()["status"] == "accepted"

    def test_cannot_befriend_yourself(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/friends/requests", json={"user_id": alice.id}, headers=alice.headers
        )
        assert response.status_code == 400

    def test_cannot_send_twice(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        client.post("/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers)
        again = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        assert again.status_code == 409

    def test_cannot_request_an_already_accepted_friend(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        response = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        assert response.status_code == 409

    def test_only_the_addressee_can_accept(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        sent = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        response = client.post(
            f"/api/v1/friends/requests/{sent.json()['id']}/accept", headers=alice.headers
        )
        assert response.status_code == 400

    def test_sender_can_withdraw(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        sent = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        withdrawn = client.delete(
            f"/api/v1/friends/requests/{sent.json()['id']}", headers=alice.headers
        )

        assert withdrawn.status_code == 200
        assert client.get("/api/v1/friends/requests", headers=bob.headers).json() == []

    def test_addressee_cannot_withdraw(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        sent = client.post(
            "/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers
        )
        response = client.delete(
            f"/api/v1/friends/requests/{sent.json()['id']}", headers=bob.headers
        )
        assert response.status_code == 400

    def test_outgoing_listing(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        client.post("/api/v1/friends/requests", json={"user_id": bob.id}, headers=alice.headers)

        outgoing = client.get(
            "/api/v1/friends/requests", params={"direction": "outgoing"}, headers=alice.headers
        ).json()
        assert len(outgoing) == 1
        assert outgoing[0]["is_incoming"] is False

    def test_unknown_person(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/friends/requests", json={"email": "ghost@example.com"}, headers=alice.headers
        )
        assert response.status_code == 404

    def test_needs_exactly_one_target(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        assert (
            client.post("/api/v1/friends/requests", json={}, headers=alice.headers).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/friends/requests",
                json={"user_id": bob.id, "email": bob.email},
                headers=alice.headers,
            ).status_code
            == 422
        )


class TestRemoveFriend:
    def test_removes_from_both_sides(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)

        response = client.delete(f"/api/v1/friends/{bob.id}", headers=alice.headers)
        assert response.status_code == 200

        assert client.get("/api/v1/friends", headers=alice.headers).json() == []
        assert client.get("/api/v1/friends", headers=bob.headers).json() == []

    def test_removing_a_non_friend_is_404(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        assert client.delete(f"/api/v1/friends/{bob.id}", headers=alice.headers).status_code == 404


class TestUserSearch:
    def test_finds_by_name_fragment(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        results = client.get(
            "/api/v1/users/search", params={"q": "bob"}, headers=alice.headers
        ).json()

        assert [result["id"] for result in results] == [bob.id]
        assert results[0]["relationship"] == "none"

    def test_finds_by_email(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        results = client.get(
            "/api/v1/users/search", params={"q": bob.email}, headers=alice.headers
        ).json()
        assert results[0]["id"] == bob.id

    def test_never_returns_yourself(self, client: TestClient, alice: Actor) -> None:
        results = client.get(
            "/api/v1/users/search", params={"q": "alice"}, headers=alice.headers
        ).json()
        assert alice.id not in [result["id"] for result in results]

    def test_reports_the_existing_relationship(
        self, client: TestClient, alice: Actor, bob: Actor, carol: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        client.post("/api/v1/friends/requests", json={"user_id": carol.id}, headers=alice.headers)

        by_id = {
            result["id"]: result["relationship"]
            for result in client.get(
                "/api/v1/users/search", params={"q": "user"}, headers=alice.headers
            ).json()
        }
        # Names are "Alice Anderson" etc, so search by the shared email prefix.
        by_id |= {
            result["id"]: result["relationship"]
            for result in client.get(
                "/api/v1/users/search", params={"q": "example.com"}, headers=alice.headers
            ).json()
        }

        assert by_id[bob.id] == "friends"
        assert by_id[carol.id] == "request_sent"

        from_carol = client.get(
            "/api/v1/users/search", params={"q": "example.com"}, headers=carol.headers
        ).json()
        assert next(r["relationship"] for r in from_carol if r["id"] == alice.id) == (
            "request_received"
        )

    def test_requires_two_characters(self, client: TestClient, alice: Actor) -> None:
        response = client.get("/api/v1/users/search", params={"q": "a"}, headers=alice.headers)
        assert response.status_code == 422

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/api/v1/users/search", params={"q": "bob"}).status_code == 401
