"""Notes, reminders and the timeline.

The themes: exactly one subject per note or reminder (enforced twice, once for the
user and once by the database), reminder status derived from the calendar, and a
timeline ordered by when things happened rather than when they were typed.
"""

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from tests.conftest import Actor

TODAY = date.today()


def open_khata(client: TestClient, actor: Actor, name: str = "Ahmed", **extra) -> dict:
    response = client.post(
        "/api/v1/khata", json={"person_name": name} | extra, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def give_loan(client: TestClient, actor: Actor, **extra) -> dict:
    body = {"counterparty_name": "Ahmed", "amount": "50000.00", "currency": "PKR"} | extra
    response = client.post("/api/v1/loans", json=body, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()


def add_note(client: TestClient, actor: Actor, **subject) -> dict:
    body = {"body": "Will return by September"} | subject
    response = client.post("/api/v1/notes", json=body, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()


def add_reminder(client: TestClient, actor: Actor, **extra) -> dict:
    body = {
        "title": "Collect from Ahmed",
        "due_date": (TODAY + timedelta(days=5)).isoformat(),
    } | extra
    response = client.post("/api/v1/reminders", json=body, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()


class TestNoteSubjects:
    def test_a_note_on_a_khata(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        note = add_note(client, alice, khata_id=khata["id"])

        assert note["subject"] == "khata"
        assert note["subject_ref"]["label"] == "Ahmed"
        assert note["subject_ref"]["href"] == f"/khata/{khata['id']}"

    def test_a_note_on_a_loan(self, client: TestClient, alice: Actor) -> None:
        loan = give_loan(client, alice)
        note = add_note(client, alice, loan_id=loan["id"])

        assert note["subject"] == "loan"
        assert note["subject_ref"]["href"] == f"/loans/{loan['id']}"

    def test_a_note_on_a_person(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        note = add_note(client, alice, person_user_id=bob.id)

        assert note["subject"] == "person"
        assert note["subject_ref"]["label"] == bob.full_name

    def test_no_subject_is_refused(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/notes", json={"body": "Floating"}, headers=alice.headers
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "subject_count"

    def test_two_subjects_are_refused(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        loan = give_loan(client, alice)
        response = client.post(
            "/api/v1/notes",
            json={"body": "Both", "khata_id": khata["id"], "loan_id": loan["id"]},
            headers=alice.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "subject_count"

    def test_the_database_refuses_what_the_api_refuses(self, db, alice: Actor) -> None:
        """The CHECK is the guarantee; the field error is only the message.

        Wrapped in a savepoint so the rollback undoes only the bad insert. Rolling
        back the whole session would also discard the fixtures this test's teardown
        still expects to be there.
        """
        from app.models.note import Note

        savepoint = db.begin_nested()
        db.add(Note(owner_id=uuid.UUID(alice.id), body="Floating"))
        with pytest.raises(IntegrityError):
            db.flush()
        savepoint.rollback()

    def test_a_blank_note_is_refused(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        response = client.post(
            "/api/v1/notes", json={"body": "   ", "khata_id": khata["id"]}, headers=alice.headers
        )
        assert response.status_code == 422

    def test_you_cannot_note_someone_elses_khata(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice)
        response = client.post(
            "/api/v1/notes", json={"body": "Nosy", "khata_id": khata["id"]}, headers=bob.headers
        )
        assert response.status_code == 404

    def test_you_cannot_note_yourself(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/notes",
            json={"body": "Mine", "person_user_id": alice.id},
            headers=alice.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "self_subject"


class TestNoteLifecycle:
    def test_notes_come_back_newest_first(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_note(client, alice, khata_id=khata["id"])
        add_note(client, alice, khata_id=khata["id"])

        page = client.get("/api/v1/notes", headers=alice.headers).json()
        assert page["total"] == 2

    def test_filtering_by_subject_and_by_id(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        loan = give_loan(client, alice)
        add_note(client, alice, khata_id=khata["id"])
        add_note(client, alice, loan_id=loan["id"])

        by_kind = client.get(
            "/api/v1/notes", params={"subject": "loan"}, headers=alice.headers
        ).json()
        assert by_kind["total"] == 1
        assert by_kind["items"][0]["loan_id"] == loan["id"]

        by_id = client.get(
            "/api/v1/notes", params={"khata_id": khata["id"]}, headers=alice.headers
        ).json()
        assert by_id["total"] == 1

    def test_searching_the_body(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_note(client, alice, khata_id=khata["id"], body="Promised by September")
        add_note(client, alice, khata_id=khata["id"], body="Paid in cash")

        page = client.get(
            "/api/v1/notes", params={"search": "september"}, headers=alice.headers
        ).json()
        assert page["total"] == 1

    def test_editing_and_deleting(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        note = add_note(client, alice, khata_id=khata["id"])

        edited = client.patch(
            f"/api/v1/notes/{note['id']}", json={"body": "Changed"}, headers=alice.headers
        )
        assert edited.status_code == 200
        assert edited.json()["body"] == "Changed"

        removed = client.delete(f"/api/v1/notes/{note['id']}", headers=alice.headers)
        assert removed.status_code == 200
        assert client.get("/api/v1/notes", headers=alice.headers).json()["total"] == 0

    def test_deleting_the_khata_takes_its_notes(
        self, client: TestClient, alice: Actor
    ) -> None:
        """A note about a deleted khata has nothing left to be about."""
        khata = open_khata(client, alice)
        add_note(client, alice, khata_id=khata["id"])

        client.delete(
            f"/api/v1/khata/{khata['id']}", params={"permanent": True}, headers=alice.headers
        )
        assert client.get("/api/v1/notes", headers=alice.headers).json()["total"] == 0

    def test_someone_elses_note_is_a_404(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice)
        note = add_note(client, alice, khata_id=khata["id"])

        assert (
            client.patch(
                f"/api/v1/notes/{note['id']}", json={"body": "x"}, headers=bob.headers
            ).status_code
            == 404
        )
        assert client.delete(f"/api/v1/notes/{note['id']}", headers=bob.headers).status_code == 404


class TestReminderStatus:
    def test_a_future_reminder_is_upcoming(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(client, alice, khata_id=khata["id"])
        assert reminder["status"] == "upcoming"
        assert reminder["days_until_due"] == 5

    def test_today_is_due_today(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(client, alice, khata_id=khata["id"], due_date=TODAY.isoformat())
        assert reminder["status"] == "due_today"
        assert reminder["days_until_due"] == 0

    def test_the_past_is_overdue(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(
            client, alice, khata_id=khata["id"], due_date=(TODAY - timedelta(days=3)).isoformat()
        )
        assert reminder["status"] == "overdue"
        assert reminder["days_until_due"] == -3

    def test_completing_and_reopening(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(client, alice, khata_id=khata["id"])

        done = client.patch(
            f"/api/v1/reminders/{reminder['id']}", json={"completed": True}, headers=alice.headers
        )
        assert done.status_code == 200
        assert done.json()["status"] == "completed"
        # A timestamp, not a flag, so "when was this done" stays answerable.
        assert done.json()["completed_at"] is not None

        reopened = client.patch(
            f"/api/v1/reminders/{reminder['id']}", json={"completed": False}, headers=alice.headers
        )
        assert reopened.json()["status"] == "upcoming"
        assert reopened.json()["completed_at"] is None

    def test_a_completed_reminder_is_hidden_by_default(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(client, alice, khata_id=khata["id"])
        client.patch(
            f"/api/v1/reminders/{reminder['id']}", json={"completed": True}, headers=alice.headers
        )

        assert client.get("/api/v1/reminders", headers=alice.headers).json()["total"] == 0
        with_done = client.get(
            "/api/v1/reminders", params={"include_completed": True}, headers=alice.headers
        ).json()
        assert with_done["total"] == 1


class TestTheTwoDates:
    def test_a_future_remind_on_holds_it_back(self, client: TestClient, alice: Actor) -> None:
        """The point of the second date: real, but not yet anyone's problem."""
        khata = open_khata(client, alice)
        reminder = add_reminder(
            client,
            alice,
            khata_id=khata["id"],
            due_date=(TODAY + timedelta(days=20)).isoformat(),
            remind_on=(TODAY + timedelta(days=15)).isoformat(),
        )
        assert reminder["is_surfaced"] is False

        surfaced = client.get(
            "/api/v1/reminders", params={"surfaced_only": True}, headers=alice.headers
        ).json()
        assert surfaced["total"] == 0

    def test_a_past_remind_on_surfaces_it(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(
            client,
            alice,
            khata_id=khata["id"],
            due_date=(TODAY + timedelta(days=20)).isoformat(),
            remind_on=(TODAY - timedelta(days=1)).isoformat(),
        )
        assert reminder["is_surfaced"] is True

    def test_no_remind_on_means_it_always_surfaces(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(client, alice, khata_id=khata["id"])
        assert reminder["remind_on"] is None
        assert reminder["is_surfaced"] is True

    def test_remind_on_after_the_due_date_is_refused(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        response = client.post(
            "/api/v1/reminders",
            json={
                "title": "Too late to be useful",
                "khata_id": khata["id"],
                "due_date": TODAY.isoformat(),
                "remind_on": (TODAY + timedelta(days=3)).isoformat(),
            },
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_moving_the_due_date_earlier_cannot_strand_the_remind_date(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Valid when it was set, invalid after the edit — so it is re-checked."""
        khata = open_khata(client, alice)
        reminder = add_reminder(
            client,
            alice,
            khata_id=khata["id"],
            due_date=(TODAY + timedelta(days=20)).isoformat(),
            remind_on=(TODAY + timedelta(days=15)).isoformat(),
        )

        response = client.patch(
            f"/api/v1/reminders/{reminder['id']}",
            json={"due_date": (TODAY + timedelta(days=5)).isoformat()},
            headers=alice.headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["details"][0]["type"] == "remind_after_due"


class TestReminderList:
    def test_soonest_due_first(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_reminder(
            client, alice, khata_id=khata["id"], title="Later",
            due_date=(TODAY + timedelta(days=10)).isoformat(),
        )
        add_reminder(
            client, alice, khata_id=khata["id"], title="Tomorrow",
            due_date=(TODAY + timedelta(days=1)).isoformat(),
        )

        page = client.get("/api/v1/reminders", headers=alice.headers).json()
        assert [item["title"] for item in page["items"]] == ["Tomorrow", "Later"]

    def test_counts_ignore_the_filters(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_reminder(
            client, alice, khata_id=khata["id"],
            due_date=(TODAY - timedelta(days=2)).isoformat(),
        )
        add_reminder(client, alice, khata_id=khata["id"])

        page = client.get(
            "/api/v1/reminders", params={"status": "upcoming"}, headers=alice.headers
        ).json()
        assert page["total"] == 1
        assert page["counts"]["open"] == 2
        assert page["counts"]["overdue"] == 1

    def test_an_amount_needs_a_currency(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        response = client.post(
            "/api/v1/reminders",
            json={
                "title": "Bare amount",
                "khata_id": khata["id"],
                "due_date": TODAY.isoformat(),
                "amount": "500.00",
            },
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_someone_elses_reminder_is_a_404(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice)
        reminder = add_reminder(client, alice, khata_id=khata["id"])
        assert (
            client.delete(
                f"/api/v1/reminders/{reminder['id']}", headers=bob.headers
            ).status_code
            == 404
        )

    def test_reminders_need_a_token(self, client: TestClient) -> None:
        assert client.get("/api/v1/reminders").status_code == 401
        assert client.get("/api/v1/notes").status_code == 401


class TestTimeline:
    def test_it_merges_every_source(
        self, client: TestClient, alice: Actor, bob: Actor, befriend, make_group
    ) -> None:
        befriend(alice, bob)
        group = make_group(alice, bob)

        client.post(
            "/api/v1/expenses",
            json={
                "group_id": group["id"], "description": "Dinner", "amount": "100.00",
                "expense_date": TODAY.isoformat(), "paid_by_id": alice.id,
                "split_type": "equal",
                "splits": [{"user_id": alice.id}, {"user_id": bob.id}],
            },
            headers=alice.headers,
        )

        khata = open_khata(client, alice, name=bob.full_name, person_user_id=bob.id)
        client.post(
            f"/api/v1/khata/{khata['id']}/entries",
            json={"entry_type": "given", "amount": "5000.00", "entry_date": TODAY.isoformat()},
            headers=alice.headers,
        )

        loan = give_loan(client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id)
        client.post(
            f"/api/v1/loans/{loan['id']}/payments",
            json={"amount": "10000.00", "payment_date": TODAY.isoformat()},
            headers=alice.headers,
        )

        add_note(client, alice, person_user_id=bob.id)

        page = client.get(f"/api/v1/people/{bob.id}/timeline", headers=alice.headers).json()
        kinds = {item["kind"] for item in page["items"]}

        assert kinds == {"expense", "khata_entry", "loan_given", "loan_payment", "note"}
        assert page["total"] == 5

    def test_it_is_ordered_by_when_things_happened(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        """A khata entry backdated to last week belongs last week, not today."""
        befriend(alice, bob)
        khata = open_khata(client, alice, name=bob.full_name, person_user_id=bob.id)

        for offset, amount in ((7, "100.00"), (1, "200.00"), (0, "300.00")):
            client.post(
                f"/api/v1/khata/{khata['id']}/entries",
                json={
                    "entry_type": "given",
                    "amount": amount,
                    "entry_date": (TODAY - timedelta(days=offset)).isoformat(),
                },
                headers=alice.headers,
            )

        page = client.get(f"/api/v1/people/{bob.id}/timeline", headers=alice.headers).json()
        dates = [item["occurred_on"] for item in page["items"]]
        assert dates == sorted(dates, reverse=True)
        assert dates[0] == TODAY.isoformat()

    def test_notes_on_a_loan_appear_on_the_persons_timeline(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        """A note on their loan is a note about this relationship."""
        befriend(alice, bob)
        loan = give_loan(client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id)
        add_note(client, alice, loan_id=loan["id"], body="Agreed to pay monthly")

        page = client.get(f"/api/v1/people/{bob.id}/timeline", headers=alice.headers).json()
        notes = [item for item in page["items"] if item["kind"] == "note"]
        assert len(notes) == 1
        assert notes[0]["detail"] == "Agreed to pay monthly"

    def test_the_timeline_pages(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        khata = open_khata(client, alice, name=bob.full_name, person_user_id=bob.id)
        for index in range(5):
            client.post(
                f"/api/v1/khata/{khata['id']}/entries",
                json={
                    "entry_type": "given",
                    "amount": "100.00",
                    "entry_date": (TODAY - timedelta(days=index)).isoformat(),
                },
                headers=alice.headers,
            )

        first = client.get(
            f"/api/v1/people/{bob.id}/timeline", params={"limit": 2}, headers=alice.headers
        ).json()
        second = client.get(
            f"/api/v1/people/{bob.id}/timeline",
            params={"limit": 2, "offset": 2},
            headers=alice.headers,
        ).json()

        assert first["total"] == 5
        assert len({i["id"] for i in first["items"]} | {i["id"] for i in second["items"]}) == 4

    def test_you_only_see_your_own_side(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        loan = give_loan(client, alice, counterparty_name=bob.full_name, counterparty_user_id=bob.id)
        assert loan["id"]

        his = client.get(f"/api/v1/people/{alice.id}/timeline", headers=bob.headers).json()
        assert his["total"] == 0
