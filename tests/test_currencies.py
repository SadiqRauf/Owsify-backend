from fastapi.testclient import TestClient

from tests.conftest import Actor


class TestCurrencyReference:
    def test_lists_the_accepted_codes(self, client: TestClient) -> None:
        response = client.get("/api/v1/currencies")

        assert response.status_code == 200
        body = response.json()
        assert body["default"] == "USD"
        assert "EUR" in body["codes"]
        assert "JPY" in body["codes"]
        assert body["codes"] == sorted(body["codes"])

    def test_is_public(self, client: TestClient) -> None:
        # A sign-up form needs it before anyone has a token.
        assert client.get("/api/v1/currencies").status_code == 200


class TestCurrencyValidation:
    def test_registration_accepts_a_valid_code(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": "jp@example.com",
                "full_name": "Yen User",
                "password": "hunter2pass",
                "currency": "jpy",
            },
        )

        assert response.status_code == 201
        assert response.json()["user"]["currency"] == "JPY"

    def test_registration_rejects_an_invented_code(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "email": "bad@example.com",
                "full_name": "Bad Currency",
                "password": "hunter2pass",
                "currency": "AAA",
            },
        )

        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert any(detail["field"] == "currency" for detail in details)

    def test_profile_update_rejects_an_invented_code(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        response = client.patch(
            "/api/v1/users/me", json={"currency": "XXQ"}, headers=auth_headers
        )
        assert response.status_code == 422

    def test_profile_update_accepts_and_upcases(
        self, client: TestClient, auth_headers: dict
    ) -> None:
        response = client.patch(
            "/api/v1/users/me", json={"currency": "gbp"}, headers=auth_headers
        )
        assert response.json()["currency"] == "GBP"

    def test_group_creation_validates_currency(self, client: TestClient, alice: Actor) -> None:
        good = client.post(
            "/api/v1/groups", json={"name": "EU trip", "currency": "eur"}, headers=alice.headers
        )
        assert good.status_code == 201
        assert good.json()["currency"] == "EUR"

        bad = client.post(
            "/api/v1/groups", json={"name": "Nope", "currency": "ZZZ"}, headers=alice.headers
        )
        assert bad.status_code == 422

    def test_personal_expense_keeps_its_own_currency(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        response = client.post(
            "/api/v1/expenses",
            json={
                "description": "Tapas",
                "amount": "30.00",
                "currency": "eur",
                "expense_date": "2026-08-01",
                "paid_by_id": alice.id,
                "split_type": "equal",
                "splits": [{"user_id": alice.id}, {"user_id": bob.id}],
            },
            headers=alice.headers,
        )

        assert response.status_code == 201
        assert response.json()["currency"] == "EUR"

    def test_group_expense_takes_the_groups_currency(
        self, client: TestClient, alice: Actor
    ) -> None:
        group = client.post(
            "/api/v1/groups", json={"name": "JP", "currency": "JPY"}, headers=alice.headers
        ).json()

        response = client.post(
            "/api/v1/expenses",
            json={
                "group_id": group["id"],
                "description": "Ramen",
                "amount": "1200.00",
                "currency": "USD",
                "expense_date": "2026-08-01",
                "paid_by_id": alice.id,
                "split_type": "equal",
                "splits": [{"user_id": alice.id}],
            },
            headers=alice.headers,
        )

        # The group's currency wins, so one group never mixes units.
        assert response.json()["currency"] == "JPY"

    def test_expense_rejects_an_invented_code(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/expenses",
            json={
                "description": "Nope",
                "amount": "10.00",
                "currency": "QQQ",
                "expense_date": "2026-08-01",
                "paid_by_id": alice.id,
                "split_type": "equal",
                "splits": [{"user_id": alice.id}],
            },
            headers=alice.headers,
        )
        assert response.status_code == 422
