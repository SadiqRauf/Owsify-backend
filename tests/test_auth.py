from fastapi.testclient import TestClient


class TestRegistration:
    def test_returns_tokens_and_the_new_user(self, client: TestClient, user_payload: dict) -> None:
        response = client.post("/api/v1/auth/register", json=user_payload)

        assert response.status_code == 201
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"] and body["refresh_token"]
        assert body["user"]["email"] == user_payload["email"]
        assert "hashed_password" not in body["user"]

    def test_rejects_a_duplicate_email(self, client: TestClient, user_payload: dict) -> None:
        client.post("/api/v1/auth/register", json=user_payload)
        response = client.post("/api/v1/auth/register", json=user_payload)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "email_already_registered"

    def test_normalises_the_email_to_lowercase(self, client: TestClient, user_payload: dict) -> None:
        response = client.post(
            "/api/v1/auth/register", json={**user_payload, "email": "ADA@Example.com"}
        )
        assert response.json()["user"]["email"] == "ada@example.com"

    def test_rejects_a_weak_password(self, client: TestClient, user_payload: dict) -> None:
        response = client.post(
            "/api/v1/auth/register", json={**user_payload, "password": "allletters"}
        )

        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert any(detail["field"] == "password" for detail in details)

    def test_rejects_a_malformed_email(self, client: TestClient, user_payload: dict) -> None:
        response = client.post("/api/v1/auth/register", json={**user_payload, "email": "nope"})
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["field"] == "email"


class TestLogin:
    def test_succeeds_with_the_right_password(
        self, client: TestClient, registered: dict, user_payload: dict
    ) -> None:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": user_payload["email"], "password": user_payload["password"]},
        )

        assert response.status_code == 200
        assert response.json()["user"]["id"] == registered["user"]["id"]

    def test_rejects_a_wrong_password(
        self, client: TestClient, registered: dict, user_payload: dict
    ) -> None:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": user_payload["email"], "password": "wrongpass1"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"

    def test_does_not_reveal_whether_the_email_exists(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "whatever1"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Incorrect email or password."

    def test_form_endpoint_powers_the_swagger_authorize_button(
        self, client: TestClient, registered: dict, user_payload: dict
    ) -> None:
        response = client.post(
            "/api/v1/auth/token",
            data={"username": user_payload["email"], "password": user_payload["password"]},
        )

        assert response.status_code == 200
        assert response.json()["access_token"]


class TestProtectedRoutes:
    def test_me_returns_the_signed_in_user(
        self, client: TestClient, auth_headers: dict, user_payload: dict
    ) -> None:
        response = client.get("/api/v1/auth/me", headers=auth_headers)

        assert response.status_code == 200
        assert response.json()["email"] == user_payload["email"]

    def test_me_requires_a_token(self, client: TestClient) -> None:
        response = client.get("/api/v1/auth/me")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_me_rejects_a_garbage_token(self, client: TestClient) -> None:
        response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
        assert response.status_code == 401

    def test_a_refresh_token_is_not_accepted_as_an_access_token(
        self, client: TestClient, registered: dict
    ) -> None:
        response = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {registered['refresh_token']}"},
        )
        assert response.status_code == 401


class TestRefresh:
    def test_rotates_the_token_pair(self, client: TestClient, registered: dict) -> None:
        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
        )

        assert response.status_code == 200
        body = response.json()
        assert body["refresh_token"] != registered["refresh_token"]
        assert body["user"]["id"] == registered["user"]["id"]

    def test_the_old_refresh_token_stops_working(
        self, client: TestClient, registered: dict
    ) -> None:
        client.post("/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]})

        replay = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
        )
        assert replay.status_code == 401

    def test_replaying_a_revoked_token_kills_every_session(
        self, client: TestClient, registered: dict
    ) -> None:
        rotated = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
        ).json()

        # Replaying the original signals a possible theft, so the new one dies too.
        client.post("/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]})

        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
        )
        assert response.status_code == 401

    def test_rejects_an_access_token(self, client: TestClient, registered: dict) -> None:
        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered["access_token"]}
        )
        assert response.status_code == 401


class TestLogout:
    def test_revokes_the_supplied_session(
        self, client: TestClient, registered: dict, auth_headers: dict
    ) -> None:
        response = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": registered["refresh_token"]},
            headers=auth_headers,
        )
        assert response.status_code == 200

        replay = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
        )
        assert replay.status_code == 401

    def test_revokes_every_session_when_no_token_is_given(
        self, client: TestClient, registered: dict, auth_headers: dict, user_payload: dict
    ) -> None:
        second = client.post(
            "/api/v1/auth/login",
            json={"email": user_payload["email"], "password": user_payload["password"]},
        ).json()

        client.post("/api/v1/auth/logout", json={}, headers=auth_headers)

        replay = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
        )
        assert replay.status_code == 401

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.post("/api/v1/auth/logout", json={}).status_code == 401
