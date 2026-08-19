from fastapi.testclient import TestClient

from app.core import email as email_module
from app.core.config import settings
from app.services import invitation as invitation_service
from tests.conftest import Actor


class TestSendInvitation:
    def test_creates_a_pending_invitation(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/friends/invitations",
            json={"email": "newcomer@example.com", "message": "Join us for the ski trip"},
            headers=alice.headers,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "newcomer@example.com"
        assert body["status"] == "pending"
        assert body["message"] == "Join us for the ski trip"
        assert body["is_expired"] is False
        assert body["invited_by"]["id"] == alice.id

    def test_lowercases_the_address(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/friends/invitations",
            json={"email": "MixedCase@Example.com"},
            headers=alice.headers,
        )
        assert response.json()["email"] == "mixedcase@example.com"

    def test_existing_account_is_a_conflict_the_ui_can_act_on(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        response = client.post(
            "/api/v1/friends/invitations", json={"email": bob.email}, headers=alice.headers
        )

        assert response.status_code == 409
        body = response.json()["error"]
        assert body["details"][0]["type"] == "account_exists"
        assert "friend request" in body["message"]

    def test_cannot_invite_yourself(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/friends/invitations", json={"email": alice.email}, headers=alice.headers
        )
        assert response.status_code in (400, 409)

    def test_cannot_invite_the_same_address_twice(self, client: TestClient, alice: Actor) -> None:
        client.post(
            "/api/v1/friends/invitations",
            json={"email": "dup@example.com"},
            headers=alice.headers,
        )
        again = client.post(
            "/api/v1/friends/invitations",
            json={"email": "dup@example.com"},
            headers=alice.headers,
        )
        assert again.status_code == 409

    def test_two_people_can_invite_the_same_address(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        first = client.post(
            "/api/v1/friends/invitations",
            json={"email": "wanted@example.com"},
            headers=alice.headers,
        )
        second = client.post(
            "/api/v1/friends/invitations",
            json={"email": "wanted@example.com"},
            headers=bob.headers,
        )
        assert first.status_code == 201
        assert second.status_code == 201

    def test_rejects_a_malformed_address(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/friends/invitations", json={"email": "not-an-email"}, headers=alice.headers
        )
        assert response.status_code == 422

    def test_requires_authentication(self, client: TestClient) -> None:
        response = client.post("/api/v1/friends/invitations", json={"email": "a@b.com"})
        assert response.status_code == 401


class TestEmailDelivery:
    def test_an_email_is_actually_sent(
        self, client: TestClient, alice: Actor, monkeypatch
    ) -> None:
        sent: list = []
        # Patch where the name is used: the service imported send_email directly.
        monkeypatch.setattr(
            invitation_service, "send_email", lambda message: sent.append(message) or True
        )

        client.post(
            "/api/v1/friends/invitations",
            json={"email": "invitee@example.com", "message": "Hi there"},
            headers=alice.headers,
        )

        assert len(sent) == 1
        message = sent[0]
        assert message.to == "invitee@example.com"
        assert "Alice Anderson" in message.subject
        assert "Hi there" in message.text_body
        # The link must carry the token and prefill the address.
        assert "/register?invite=" in message.text_body
        assert "invitee@example.com" in message.text_body

    def test_the_html_part_is_included(
        self, client: TestClient, alice: Actor, monkeypatch
    ) -> None:
        sent: list = []
        monkeypatch.setattr(
            invitation_service, "send_email", lambda message: sent.append(message) or True
        )

        client.post(
            "/api/v1/friends/invitations",
            json={"email": "html@example.com"},
            headers=alice.headers,
        )

        rendered = sent[0].as_message()
        assert rendered.is_multipart()
        assert {part.get_content_type() for part in rendered.walk()} >= {
            "text/plain",
            "text/html",
        }

    def test_a_send_failure_does_not_fail_the_request(
        self, client: TestClient, alice: Actor, monkeypatch
    ) -> None:
        def explode(_message):
            raise RuntimeError("SMTP is down")

        # The backend registry holds direct references, so replace the entry itself.
        monkeypatch.setitem(email_module._BACKENDS, "console", explode)

        response = client.post(
            "/api/v1/friends/invitations",
            json={"email": "unreachable@example.com"},
            headers=alice.headers,
        )
        # The invitation exists and can be resent; the outage is not the user's problem.
        assert response.status_code == 201

        listed = client.get("/api/v1/friends/invitations", headers=alice.headers).json()
        assert listed[0]["email"] == "unreachable@example.com"

    def test_send_email_reports_failure_rather_than_raising(self, monkeypatch) -> None:
        def explode(_message):
            raise RuntimeError("SMTP is down")

        monkeypatch.setitem(email_module._BACKENDS, "console", explode)

        result = email_module.send_email(
            email_module.Email(to="a@b.com", subject="s", text_body="t")
        )
        assert result is False


class TestManageInvitations:
    def test_lists_what_you_sent(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        client.post(
            "/api/v1/friends/invitations", json={"email": "one@example.com"}, headers=alice.headers
        )
        client.post(
            "/api/v1/friends/invitations", json={"email": "two@example.com"}, headers=bob.headers
        )

        mine = client.get("/api/v1/friends/invitations", headers=alice.headers).json()
        assert [invite["email"] for invite in mine] == ["one@example.com"]

    def test_cancel_then_reinvite(self, client: TestClient, alice: Actor) -> None:
        created = client.post(
            "/api/v1/friends/invitations",
            json={"email": "fickle@example.com"},
            headers=alice.headers,
        ).json()

        cancelled = client.delete(
            f"/api/v1/friends/invitations/{created['id']}", headers=alice.headers
        )
        assert cancelled.status_code == 200
        assert client.get("/api/v1/friends/invitations", headers=alice.headers).json() == []

        # Cancelling frees the address up again.
        again = client.post(
            "/api/v1/friends/invitations",
            json={"email": "fickle@example.com"},
            headers=alice.headers,
        )
        assert again.status_code == 201

    def test_resend(self, client: TestClient, alice: Actor) -> None:
        created = client.post(
            "/api/v1/friends/invitations",
            json={"email": "again@example.com"},
            headers=alice.headers,
        ).json()

        resent = client.post(
            f"/api/v1/friends/invitations/{created['id']}/resend", headers=alice.headers
        )
        assert resent.status_code == 200
        assert resent.json()["expires_at"] >= created["expires_at"]

    def test_cannot_touch_someone_elses_invitation(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        created = client.post(
            "/api/v1/friends/invitations",
            json={"email": "theirs@example.com"},
            headers=alice.headers,
        ).json()

        assert (
            client.delete(
                f"/api/v1/friends/invitations/{created['id']}", headers=bob.headers
            ).status_code
            == 404
        )


class TestRedemption:
    def test_registering_at_an_invited_address_creates_the_friendship(
        self, client: TestClient, alice: Actor
    ) -> None:
        client.post(
            "/api/v1/friends/invitations",
            json={"email": "newbie@example.com"},
            headers=alice.headers,
        )

        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": "newbie@example.com",
                "full_name": "New Bie",
                "password": "hunter2pass",
            },
        )
        assert registered.status_code == 201
        newbie_headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

        # Both sides are friends immediately, with no request to accept.
        newbie_friends = client.get("/api/v1/friends", headers=newbie_headers).json()
        assert [friend["user"]["id"] for friend in newbie_friends] == [alice.id]

        alice_friends = client.get("/api/v1/friends", headers=alice.headers).json()
        assert registered.json()["user"]["id"] in [f["user"]["id"] for f in alice_friends]

    def test_the_invitation_is_marked_accepted(self, client: TestClient, alice: Actor) -> None:
        client.post(
            "/api/v1/friends/invitations",
            json={"email": "accepted@example.com"},
            headers=alice.headers,
        )
        client.post(
            "/api/v1/auth/register",
            json={
                "email": "accepted@example.com",
                "full_name": "Acc Epted",
                "password": "hunter2pass",
            },
        )

        invites = client.get("/api/v1/friends/invitations", headers=alice.headers).json()
        assert invites[0]["status"] == "accepted"
        assert invites[0]["accepted_at"] is not None

    def test_a_cancelled_invitation_is_not_redeemed(
        self, client: TestClient, alice: Actor
    ) -> None:
        created = client.post(
            "/api/v1/friends/invitations",
            json={"email": "gone@example.com"},
            headers=alice.headers,
        ).json()
        client.delete(f"/api/v1/friends/invitations/{created['id']}", headers=alice.headers)

        client.post(
            "/api/v1/auth/register",
            json={"email": "gone@example.com", "full_name": "G One", "password": "hunter2pass"},
        )

        assert client.get("/api/v1/friends", headers=alice.headers).json() == []

    def test_invitations_from_several_people_all_redeem(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        for actor in (alice, bob):
            client.post(
                "/api/v1/friends/invitations",
                json={"email": "popular@example.com"},
                headers=actor.headers,
            )

        registered = client.post(
            "/api/v1/auth/register",
            json={
                "email": "popular@example.com",
                "full_name": "Pop Ular",
                "password": "hunter2pass",
            },
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

        friends = client.get("/api/v1/friends", headers=headers).json()
        assert {friend["user"]["id"] for friend in friends} == {alice.id, bob.id}

    def test_registering_with_no_invitation_is_unaffected(self, client: TestClient) -> None:
        registered = client.post(
            "/api/v1/auth/register",
            json={"email": "solo@example.com", "full_name": "So Lo", "password": "hunter2pass"},
        )
        assert registered.status_code == 201
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        assert client.get("/api/v1/friends", headers=headers).json() == []


class TestDeliveryReporting:
    """The response must say how mail was actually handled.

    A stubbed backend that reports success is how you end up waiting for an email
    that was never going to arrive, so this is pinned rather than assumed.
    """

    def test_console_backend_is_reported_honestly(
        self, client: TestClient, alice: Actor, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_BACKEND", "console")

        response = client.post(
            "/api/v1/friends/invitations",
            json={"email": "logged@example.com"},
            headers=alice.headers,
        )

        assert response.status_code == 201
        assert response.json()["delivery"] == "console"

    def test_smtp_backend_reports_email(
        self, client: TestClient, alice: Actor, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_BACKEND", "smtp")
        monkeypatch.setattr(invitation_service, "send_email", lambda _message: True)

        response = client.post(
            "/api/v1/friends/invitations",
            json={"email": "posted@example.com"},
            headers=alice.headers,
        )
        assert response.json()["delivery"] == "email"

    def test_file_backend_reports_file(
        self, client: TestClient, alice: Actor, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "EMAIL_BACKEND", "file")
        monkeypatch.setattr(invitation_service, "send_email", lambda _message: True)

        response = client.post(
            "/api/v1/friends/invitations",
            json={"email": "written@example.com"},
            headers=alice.headers,
        )
        assert response.json()["delivery"] == "file"

    def test_listing_serialises_without_error(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Regression: `delivery` is not a column, so it cannot be passed to
        model_validate — doing so raised TypeError and turned this into a 500."""
        client.post(
            "/api/v1/friends/invitations",
            json={"email": "listed@example.com"},
            headers=alice.headers,
        )

        response = client.get("/api/v1/friends/invitations", headers=alice.headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body) == 1
        assert body[0]["email"] == "listed@example.com"
        assert body[0]["delivery"] in {"email", "console", "file"}

    def test_resend_also_reports_delivery(self, client: TestClient, alice: Actor) -> None:
        created = client.post(
            "/api/v1/friends/invitations",
            json={"email": "again2@example.com"},
            headers=alice.headers,
        ).json()

        response = client.post(
            f"/api/v1/friends/invitations/{created['id']}/resend", headers=alice.headers
        )
        assert response.status_code == 200
        assert "delivery" in response.json()
