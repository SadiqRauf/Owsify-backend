from fastapi.testclient import TestClient


class TestProfile:
    def test_reads_own_profile(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get("/api/v1/users/me", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["full_name"] == "Ada Lovelace"

    def test_updates_name_and_currency(self, client: TestClient, auth_headers: dict) -> None:
        response = client.patch(
            "/api/v1/users/me",
            json={"full_name": "Ada King", "currency": "eur"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["full_name"] == "Ada King"
        assert body["currency"] == "EUR"

    def test_a_patch_leaves_unsent_fields_alone(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        response = client.patch(
            "/api/v1/users/me", json={"currency": "GBP"}, headers=auth_headers
        )
        assert response.json()["full_name"] == "Ada Lovelace"

    def test_rejects_a_blank_name(self, client: TestClient, auth_headers: dict) -> None:
        response = client.patch(
            "/api/v1/users/me", json={"full_name": "   "}, headers=auth_headers
        )
        assert response.status_code == 422

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.get("/api/v1/users/me").status_code == 401


class TestPasswordChange:
    def test_changes_the_password_and_revokes_sessions(
        self, client: TestClient, registered: dict, auth_headers: dict, user_payload: dict
    ) -> None:
        response = client.post(
            "/api/v1/users/me/password",
            json={
                "current_password": user_payload["password"],
                "new_password": "newpassword9",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200

        # Old refresh token is dead...
        replay = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
        )
        assert replay.status_code == 401

        # ...and the new password is the one that now works.
        login = client.post(
            "/api/v1/auth/login",
            json={"email": user_payload["email"], "password": "newpassword9"},
        )
        assert login.status_code == 200

    def test_rejects_a_wrong_current_password(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        response = client.post(
            "/api/v1/users/me/password",
            json={"current_password": "notitpass1", "new_password": "newpassword9"},
            headers=auth_headers,
        )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"

    def test_rejects_reusing_the_same_password(
        self, client: TestClient, auth_headers: dict, user_payload: dict
    ) -> None:
        response = client.post(
            "/api/v1/users/me/password",
            json={
                "current_password": user_payload["password"],
                "new_password": user_payload["password"],
            },
            headers=auth_headers,
        )
        assert response.status_code == 400


class TestUserLookup:
    def test_reads_another_user_by_id(
        self, client: TestClient, registered: dict, auth_headers: dict
    ) -> None:
        response = client.get(f"/api/v1/users/{registered['user']['id']}", headers=auth_headers)
        assert response.status_code == 200

    def test_returns_404_for_an_unknown_id(self, client: TestClient, auth_headers: dict) -> None:
        response = client.get(
            "/api/v1/users/00000000-0000-0000-0000-000000000000", headers=auth_headers
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_rejects_a_malformed_id(self, client: TestClient, auth_headers: dict) -> None:
        assert client.get("/api/v1/users/not-a-uuid", headers=auth_headers).status_code == 422
