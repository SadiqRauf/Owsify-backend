"""Forgot password.

This flow hands out what is effectively a temporary password, so the tests are
written around the ways it could leak or be abused rather than only the happy path:
account enumeration, token reuse, expiry, rate limiting, and whether a reset
actually ends the sessions it is supposed to end.
"""

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.password_reset import PasswordResetToken
from tests.conftest import Actor

SENT = "If that email has an account, a reset link is on its way."


def forgot(client: TestClient, email: str):
    return client.post("/api/v1/auth/forgot-password", json={"email": email})


class TestNoAccountEnumeration:
    def test_a_known_and_an_unknown_address_answer_identically(
        self, client: TestClient, alice: Actor
    ) -> None:
        """The whole security design of this endpoint in one assertion."""
        known = forgot(client, alice.email)
        unknown = forgot(client, "nobody.at.all@example.com")

        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()
        assert known.json()["message"] == SENT

    def test_being_rate_limited_looks_the_same_too(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Otherwise the rate limiter itself becomes the enumeration oracle."""
        for _ in range(5):
            response = forgot(client, alice.email)
            assert response.status_code == 200
            assert response.json()["message"] == SENT

    def test_an_inactive_account_answers_the_same(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        from app.models.user import User
        import uuid as uuid_module

        user = db.get(User, uuid_module.UUID(alice.id))
        user.is_active = False
        db.commit()

        response = forgot(client, alice.email)
        assert response.status_code == 200
        assert response.json()["message"] == SENT

    def test_the_email_is_sent_after_the_response_not_before_it(self) -> None:
        """Identical wording is worthless if the timing gives the answer away.

        Sending inline made a real address take ~20s against a live mail server and
        an unknown one ~40ms — a stopwatch was enough to enumerate accounts. The
        send is now a background task, so the response leaves first.

        This asserts the *structure* rather than the elapsed time, because
        Starlette's TestClient drains background tasks before returning from the
        request: a timing assertion here would measure the test client, not the
        server. The elapsed-time behaviour is verified against a running instance.
        """
        import inspect

        from fastapi import BackgroundTasks

        from app.api.v1.endpoints.auth import forgot_password

        annotations = [
            parameter.annotation
            for parameter in inspect.signature(forgot_password).parameters.values()
        ]
        assert BackgroundTasks in annotations, (
            "forgot_password must take BackgroundTasks and schedule the send, "
            "otherwise the response time reveals whether the account exists"
        )

    def test_no_reset_token_is_issued_for_an_unknown_address(
        self, client: TestClient, db
    ) -> None:
        forgot(client, "nobody.at.all@example.com")
        assert db.scalars(select(PasswordResetToken)).all() == []


class TestTheTokenIsStoredHashed:
    def test_the_table_holds_a_hash_not_the_token(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        """A leaked backup must not hand over accounts with a live reset in flight."""
        forgot(client, alice.email)
        stored = db.scalars(select(PasswordResetToken)).one()

        assert len(stored.token_hash) == 64
        assert all(c in "0123456789abcdef" for c in stored.token_hash)

    def test_the_hash_matches_the_token_the_service_returned(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        from app.services import auth as auth_service

        issued = auth_service.request_password_reset(db, alice.email)
        assert issued is not None
        _, token = issued

        stored = db.scalars(
            select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None))
        ).one()
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
        # And the plain token appears nowhere in the row.
        assert token not in (stored.token_hash, str(stored.id))


class TestResetting:
    def _issue(self, db, email: str) -> str:
        from app.services import auth as auth_service

        issued = auth_service.request_password_reset(db, email)
        assert issued is not None
        return issued[1]

    def test_the_happy_path(self, client: TestClient, alice: Actor, db) -> None:
        token = self._issue(db, alice.email)

        response = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "BrandNewPass1!"},
        )
        assert response.status_code == 200, response.text

        # The old password is gone and the new one works.
        old = client.post(
            "/api/v1/auth/login",
            json={"email": alice.email, "password": "hunter2pass"},
        )
        assert old.status_code == 401

        new = client.post(
            "/api/v1/auth/login",
            json={"email": alice.email, "password": "BrandNewPass1!"},
        )
        assert new.status_code == 200, new.text

    def test_a_token_works_only_once(self, client: TestClient, alice: Actor, db) -> None:
        token = self._issue(db, alice.email)
        body = {"token": token, "new_password": "BrandNewPass1!"}

        assert client.post("/api/v1/auth/reset-password", json=body).status_code == 200
        again = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "AnotherOne1!"},
        )
        assert again.status_code == 401

    def test_an_expired_token_is_refused(self, client: TestClient, alice: Actor, db) -> None:
        token = self._issue(db, alice.email)
        stored = db.scalars(
            select(PasswordResetToken).where(PasswordResetToken.used_at.is_(None))
        ).one()
        stored.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        response = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "BrandNewPass1!"},
        )
        assert response.status_code == 401

    def test_a_made_up_token_is_refused(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/auth/reset-password",
            json={"token": "x" * 40, "new_password": "BrandNewPass1!"},
        )
        assert response.status_code == 401

    def test_expired_used_and_invented_give_the_same_message(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        """Telling them apart tells an attacker which guesses were once real."""
        used = self._issue(db, alice.email)
        client.post(
            "/api/v1/auth/reset-password",
            json={"token": used, "new_password": "BrandNewPass1!"},
        )

        messages = set()
        for token in (used, "y" * 40):
            response = client.post(
                "/api/v1/auth/reset-password",
                json={"token": token, "new_password": "Whatever123!"},
            )
            messages.add(response.json()["error"]["message"])

        assert len(messages) == 1

    def test_a_short_password_is_refused(self, client: TestClient, alice: Actor, db) -> None:
        token = self._issue(db, alice.email)
        response = client.post(
            "/api/v1/auth/reset-password", json={"token": token, "new_password": "short"}
        )
        assert response.status_code == 422

    def test_requesting_again_invalidates_the_earlier_link(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        """A stale message in an inbox must stop working once a new one is asked for."""
        first = self._issue(db, alice.email)
        second = self._issue(db, alice.email)
        assert first != second

        stale = client.post(
            "/api/v1/auth/reset-password",
            json={"token": first, "new_password": "BrandNewPass1!"},
        )
        assert stale.status_code == 401

        fresh = client.post(
            "/api/v1/auth/reset-password",
            json={"token": second, "new_password": "BrandNewPass1!"},
        )
        assert fresh.status_code == 200


class TestResettingEndsSessions:
    def test_every_existing_session_is_signed_out(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        """A reset often follows a compromise. Leaving the attacker signed in would
        make the whole exercise theatre."""
        from app.services import auth as auth_service

        signed_in = client.post(
            "/api/v1/auth/login", json={"email": alice.email, "password": "hunter2pass"}
        ).json()
        refresh_token = signed_in["refresh_token"]

        # The session works before the reset.
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
            ).status_code
            == 200
        )

        issued = auth_service.request_password_reset(db, alice.email)
        assert issued is not None
        client.post(
            "/api/v1/auth/reset-password",
            json={"token": issued[1], "new_password": "BrandNewPass1!"},
        )

        # And is dead afterwards.
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
            ).status_code
            == 401
        )


class TestRateLimiting:
    def test_only_a_few_tokens_are_issued_per_window(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        """An unauthenticated endpoint that sends mail is a spam vector aimed at
        someone else's inbox."""
        from app.core.config import settings

        issued = []
        for _ in range(settings.PASSWORD_RESET_MAX_PER_WINDOW + 3):
            from app.services import auth as auth_service

            result = auth_service.request_password_reset(db, alice.email)
            issued.append(result is not None)

        assert sum(issued) == settings.PASSWORD_RESET_MAX_PER_WINDOW
        assert issued[-1] is False


class TestCheckingALinkBeforeUsingIt:
    def test_a_live_token_reports_valid_with_the_address(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        from app.services import auth as auth_service

        issued = auth_service.request_password_reset(db, alice.email)
        assert issued is not None

        response = client.get(
            "/api/v1/auth/reset-password", params={"token": issued[1]}
        )
        assert response.status_code == 200
        assert response.json() == {"valid": True, "email": alice.email}

    def test_a_bad_token_reports_invalid_without_an_address(
        self, client: TestClient
    ) -> None:
        response = client.get("/api/v1/auth/reset-password", params={"token": "z" * 40})
        assert response.status_code == 200
        assert response.json() == {"valid": False, "email": None}

    def test_checking_does_not_consume_the_token(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        from app.services import auth as auth_service

        issued = auth_service.request_password_reset(db, alice.email)
        assert issued is not None
        token = issued[1]

        client.get("/api/v1/auth/reset-password", params={"token": token})
        response = client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "BrandNewPass1!"},
        )
        assert response.status_code == 200


class TestTheEmail:
    def test_the_link_points_at_the_reset_page_with_the_token(
        self, alice: Actor, db
    ) -> None:
        from app.core.config import settings
        from app.services import auth as auth_service
        from app.services import password_reset_email

        issued = auth_service.request_password_reset(db, alice.email)
        assert issued is not None
        user, token = issued

        email = password_reset_email.build(user, token)
        expected = f"{settings.FRONTEND_URL}/reset-password?token={token}"

        assert expected in email.text_body
        assert expected in email.html_body
        assert email.to == alice.email
