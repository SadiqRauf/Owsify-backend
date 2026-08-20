import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.models.khata import KhataEntry, KhataEntryType
from app.services.khata_entry import signed_amount_sql
from tests.conftest import Actor

TODAY = date.today().isoformat()


def open_khata(client: TestClient, actor: Actor, name: str = "Ahmed", **extra) -> dict:
    response = client.post(
        "/api/v1/khata", json={"person_name": name} | extra, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_entry(
    client: TestClient, actor: Actor, khata_id: str, entry_type: str, amount: str, **extra
) -> dict:
    body = {"entry_type": entry_type, "amount": amount, "entry_date": TODAY} | extra
    response = client.post(
        f"/api/v1/khata/{khata_id}/entries", json=body, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def ledger(client: TestClient, actor: Actor, khata_id: str, **params) -> dict:
    return client.get(
        f"/api/v1/khata/{khata_id}/entries", params=params, headers=actor.headers
    ).json()


class TestTheWorkedExample:
    def test_given_ten_thousand_received_three_leaves_seven(
        self, client: TestClient, alice: Actor
    ) -> None:
        """The arithmetic from the brief, end to end."""
        khata = open_khata(client, alice)

        add_entry(client, alice, khata["id"], "given", "10000.00", description="Laptop")
        add_entry(
            client, alice, khata["id"], "received", "3000.00", description="Payment received"
        )

        page = ledger(client, alice, khata["id"])
        assert Decimal(page["balance"]) == Decimal("7000.00")
        assert Decimal(page["totals"]["given"]) == Decimal("10000.00")
        assert Decimal(page["totals"]["received"]) == Decimal("3000.00")

        # And the khata itself agrees, since both read the same rows.
        detail = client.get(f"/api/v1/khata/{khata['id']}", headers=alice.headers).json()
        assert Decimal(detail["balance"]) == Decimal("7000.00")
        assert detail["entry_count"] == 2


class TestEntryTypes:
    def test_given_increases_and_received_decreases(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)

        given = add_entry(client, alice, khata["id"], "given", "500.00")
        assert Decimal(given["signed_amount"]) == Decimal("500.00")

        received = add_entry(client, alice, khata["id"], "received", "200.00")
        assert Decimal(received["signed_amount"]) == Decimal("-200.00")

        assert Decimal(ledger(client, alice, khata["id"])["balance"]) == Decimal("300.00")

    def test_an_adjustment_may_go_either_way(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "1000.00")

        add_entry(
            client, alice, khata["id"], "adjustment", "-150.00", description="Overcharged"
        )
        assert Decimal(ledger(client, alice, khata["id"])["balance"]) == Decimal("850.00")

        add_entry(client, alice, khata["id"], "adjustment", "50.00", description="Rounding")
        assert Decimal(ledger(client, alice, khata["id"])["balance"]) == Decimal("900.00")

    def test_only_an_adjustment_may_be_negative(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Direction belongs to the type; a negative GIVEN would be a second way
        to write a RECEIVED."""
        khata = open_khata(client, alice)

        for entry_type in ("given", "received"):
            response = client.post(
                f"/api/v1/khata/{khata['id']}/entries",
                json={"entry_type": entry_type, "amount": "-100.00", "entry_date": TODAY},
                headers=alice.headers,
            )
            assert response.status_code == 422, entry_type

    def test_zero_is_rejected(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        response = client.post(
            f"/api/v1/khata/{khata['id']}/entries",
            json={"entry_type": "given", "amount": "0", "entry_date": TODAY},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_the_database_refuses_what_the_api_refuses(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        """The CHECK constraint is the guarantee; API validation is the message.
        If the constraint were missing, a direct write would slip through."""
        import uuid as uuid_module

        from sqlalchemy.exc import IntegrityError

        khata = open_khata(client, alice)

        db.add(
            KhataEntry(
                khata_id=uuid_module.UUID(khata["id"]),
                entry_type=KhataEntryType.GIVEN,
                amount=Decimal("-5.00"),
                entry_date=date.today(),
                created_by_id=uuid_module.UUID(alice.id),
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()

    def test_signed_amount_agrees_between_python_and_sql(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        """The rule exists twice — in the model property and in a CASE expression.
        This is what stops them drifting."""
        from sqlalchemy import select

        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "700.00")
        add_entry(client, alice, khata["id"], "received", "250.00")
        add_entry(client, alice, khata["id"], "adjustment", "-30.00")

        rows = db.execute(
            select(KhataEntry, signed_amount_sql()).where(
                KhataEntry.khata_id == uuid.UUID(khata["id"])
            )
        ).all()

        assert len(rows) == 3
        for entry, sql_signed in rows:
            assert entry.signed_amount == Decimal(sql_signed), entry.entry_type


class TestRunningBalance:
    def test_each_entry_carries_the_balance_after_it(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        base = date.today() - timedelta(days=5)

        for offset, entry_type, amount in (
            (0, "given", "10000.00"),
            (1, "received", "3000.00"),
            (2, "given", "500.00"),
        ):
            add_entry(
                client,
                alice,
                khata["id"],
                entry_type,
                amount,
                entry_date=(base + timedelta(days=offset)).isoformat(),
            )

        # Newest first, so the running balances read downwards as the reverse of
        # how the ledger accumulated.
        items = ledger(client, alice, khata["id"])["items"]
        assert [Decimal(i["running_balance"]) for i in items] == [
            Decimal("7500.00"),
            Decimal("7000.00"),
            Decimal("10000.00"),
        ]

    def test_running_balance_survives_pagination(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Summing only the rows on the page would restart the total at every page."""
        khata = open_khata(client, alice)
        base = date.today() - timedelta(days=10)

        for index in range(6):
            add_entry(
                client,
                alice,
                khata["id"],
                "given",
                "100.00",
                entry_date=(base + timedelta(days=index)).isoformat(),
            )

        first = ledger(client, alice, khata["id"], limit=3, offset=0)
        second = ledger(client, alice, khata["id"], limit=3, offset=3)

        assert first["total"] == second["total"] == 6
        # Newest page ends at 600 and counts down; the older page continues from 300.
        assert [Decimal(i["running_balance"]) for i in first["items"]] == [
            Decimal("600.00"),
            Decimal("500.00"),
            Decimal("400.00"),
        ]
        assert [Decimal(i["running_balance"]) for i in second["items"]] == [
            Decimal("300.00"),
            Decimal("200.00"),
            Decimal("100.00"),
        ]

    def test_running_balance_survives_a_date_filter(
        self, client: TestClient, alice: Actor
    ) -> None:
        """A filtered view must still show the true balance at each line, not a
        total restarted from the first visible row."""
        khata = open_khata(client, alice)
        base = date.today() - timedelta(days=10)

        add_entry(
            client, alice, khata["id"], "given", "1000.00",
            entry_date=base.isoformat(),
        )
        add_entry(
            client, alice, khata["id"], "given", "250.00",
            entry_date=(base + timedelta(days=8)).isoformat(),
        )

        recent = ledger(
            client,
            alice,
            khata["id"],
            start_date=(base + timedelta(days=5)).isoformat(),
        )

        assert recent["total"] == 1
        # 1250, not 250: the earlier entry still happened.
        assert Decimal(recent["items"][0]["running_balance"]) == Decimal("1250.00")
        # And the khata's own balance ignores the filter entirely.
        assert Decimal(recent["balance"]) == Decimal("1250.00")

    def test_entries_on_the_same_day_get_a_stable_order(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        for _ in range(4):
            add_entry(client, alice, khata["id"], "given", "10.00")

        first = [i["id"] for i in ledger(client, alice, khata["id"])["items"]]
        second = [i["id"] for i in ledger(client, alice, khata["id"])["items"]]
        assert first == second

        balances = [
            Decimal(i["running_balance"]) for i in ledger(client, alice, khata["id"])["items"]
        ]
        assert balances == [Decimal("40.00"), Decimal("30.00"), Decimal("20.00"), Decimal("10.00")]


class TestEditAndDelete:
    def test_editing_an_amount_moves_the_balance(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        entry = add_entry(client, alice, khata["id"], "given", "1000.00")

        response = client.patch(
            f"/api/v1/khata/entries/{entry['id']}",
            json={"amount": "1500.00"},
            headers=alice.headers,
        )
        assert response.status_code == 200
        assert Decimal(ledger(client, alice, khata["id"])["balance"]) == Decimal("1500.00")

    def test_changing_the_type_flips_the_direction(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        entry = add_entry(client, alice, khata["id"], "given", "800.00")

        client.patch(
            f"/api/v1/khata/entries/{entry['id']}",
            json={"entry_type": "received"},
            headers=alice.headers,
        )
        assert Decimal(ledger(client, alice, khata["id"])["balance"]) == Decimal("-800.00")

    def test_deleting_removes_its_effect(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "1000.00")
        removable = add_entry(client, alice, khata["id"], "given", "400.00")

        response = client.delete(
            f"/api/v1/khata/entries/{removable['id']}", headers=alice.headers
        )
        assert response.status_code == 200

        page = ledger(client, alice, khata["id"])
        assert Decimal(page["balance"]) == Decimal("1000.00")
        assert page["total"] == 1

    def test_an_edit_cannot_make_a_given_negative(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        entry = add_entry(client, alice, khata["id"], "given", "100.00")

        response = client.patch(
            f"/api/v1/khata/entries/{entry['id']}",
            json={"amount": "-100.00"},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_rejects_an_empty_patch(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        entry = add_entry(client, alice, khata["id"], "given", "100.00")

        response = client.patch(
            f"/api/v1/khata/entries/{entry['id']}", json={}, headers=alice.headers
        )
        assert response.status_code == 422


class TestFilteringAndPaging:
    def test_filter_by_type(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "100.00")
        add_entry(client, alice, khata["id"], "given", "200.00")
        add_entry(client, alice, khata["id"], "received", "50.00")

        given = ledger(client, alice, khata["id"], entry_type="given")
        assert given["total"] == 2
        # The khata's balance is not what the filter shows.
        assert Decimal(given["balance"]) == Decimal("250.00")

    def test_search_the_note(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "100.00", description="Laptop charger")
        add_entry(client, alice, khata["id"], "given", "200.00", description="Rice bag")

        page = ledger(client, alice, khata["id"], search="laptop")
        assert page["total"] == 1
        assert page["items"][0]["description"] == "Laptop charger"

    def test_date_range(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        base = date.today() - timedelta(days=20)
        for offset in (0, 10, 19):
            add_entry(
                client, alice, khata["id"], "given", "10.00",
                entry_date=(base + timedelta(days=offset)).isoformat(),
            )

        page = ledger(
            client,
            alice,
            khata["id"],
            start_date=(base + timedelta(days=5)).isoformat(),
            end_date=(base + timedelta(days=15)).isoformat(),
        )
        assert page["total"] == 1

    def test_rejects_a_backwards_range(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        response = client.get(
            f"/api/v1/khata/{khata['id']}/entries",
            params={"start_date": "2026-08-20", "end_date": "2026-01-01"},
            headers=alice.headers,
        )
        assert response.status_code == 400

    def test_a_future_date_is_rejected(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        response = client.post(
            f"/api/v1/khata/{khata['id']}/entries",
            json={"entry_type": "given", "amount": "10.00", "entry_date": "2099-01-01"},
            headers=alice.headers,
        )
        assert response.status_code == 422


class TestPermissions:
    def test_cannot_add_to_someone_elses_khata(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice)

        response = client.post(
            f"/api/v1/khata/{khata['id']}/entries",
            json={"entry_type": "given", "amount": "100.00", "entry_date": TODAY},
            headers=bob.headers,
        )
        assert response.status_code == 404

    def test_cannot_read_someone_elses_ledger(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "100.00")

        assert client.get(
            f"/api/v1/khata/{khata['id']}/entries", headers=bob.headers
        ).status_code == 404

    def test_cannot_edit_or_delete_someone_elses_entry(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice)
        entry = add_entry(client, alice, khata["id"], "given", "100.00")

        assert client.patch(
            f"/api/v1/khata/entries/{entry['id']}",
            json={"amount": "1.00"},
            headers=bob.headers,
        ).status_code == 404
        assert client.delete(
            f"/api/v1/khata/entries/{entry['id']}", headers=bob.headers
        ).status_code == 404

    def test_requires_authentication(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        assert client.get(f"/api/v1/khata/{khata['id']}/entries").status_code == 401


class TestRouting:
    def test_the_entry_route_is_not_swallowed_by_the_khata_route(
        self, client: TestClient, alice: Actor
    ) -> None:
        """`/khata/entries/{id}` must not be matched by `/khata/{khata_id}`, which
        would read "entries" as a malformed uuid and answer 422."""
        khata = open_khata(client, alice)
        entry = add_entry(client, alice, khata["id"], "given", "100.00")

        response = client.patch(
            f"/api/v1/khata/entries/{entry['id']}",
            json={"description": "Reached the right route"},
            headers=alice.headers,
        )
        assert response.status_code == 200
        assert response.json()["description"] == "Reached the right route"
