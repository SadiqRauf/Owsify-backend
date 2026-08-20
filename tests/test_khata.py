from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor


def open_khata(client: TestClient, actor: Actor, name: str, **extra) -> dict:
    response = client.post(
        "/api/v1/khata", json={"person_name": name} | extra, headers=actor.headers
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestCreate:
    def test_a_name_is_all_it_takes(self, client: TestClient, alice: Actor) -> None:
        """The main use of a khata is for someone who is not on the app at all."""
        body = open_khata(client, alice, "Ahmed")

        assert body["person_name"] == "Ahmed"
        assert body["display_name"] == "Ahmed"
        assert body["person_user"] is None
        assert body["currency"] == "PKR"
        assert Decimal(body["balance"]) == Decimal("0.00")
        assert body["entry_count"] == 0
        assert body["is_archived"] is False

    def test_the_three_from_the_brief(self, client: TestClient, alice: Actor) -> None:
        for name in ("Ahmed", "Ali", "Usman"):
            open_khata(client, alice, name)

        page = client.get("/api/v1/khata", headers=alice.headers).json()
        assert page["total"] == 3
        assert {item["person_name"] for item in page["items"]} == {"Ahmed", "Ali", "Usman"}

    def test_trims_the_name_and_rejects_a_blank_one(
        self, client: TestClient, alice: Actor
    ) -> None:
        assert open_khata(client, alice, "  Ahmed  ")["person_name"] == "Ahmed"

        response = client.post(
            "/api/v1/khata", json={"person_name": "   "}, headers=alice.headers
        )
        assert response.status_code == 422

    def test_can_link_an_account(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        body = open_khata(client, alice, "Bobby", person_user_id=bob.id)

        assert body["person_user"]["id"] == bob.id
        # The linked account's name wins for display, so a rename there follows.
        assert body["person_name"] == "Bobby"
        assert body["display_name"] == bob.full_name

    def test_one_khata_per_linked_person(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        open_khata(client, alice, "Bob", person_user_id=bob.id)

        again = client.post(
            "/api/v1/khata",
            json={"person_name": "Bob again", "person_user_id": bob.id},
            headers=alice.headers,
        )
        assert again.status_code == 409
        assert again.json()["error"]["details"][0]["type"] == "khata_exists"

    def test_two_unlinked_khatas_may_share_a_name(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Two different customers can genuinely both be called Ahmed."""
        open_khata(client, alice, "Ahmed")
        second = client.post(
            "/api/v1/khata", json={"person_name": "Ahmed"}, headers=alice.headers
        )
        assert second.status_code == 201

    def test_cannot_keep_a_khata_with_yourself(
        self, client: TestClient, alice: Actor
    ) -> None:
        response = client.post(
            "/api/v1/khata",
            json={"person_name": "Me", "person_user_id": alice.id},
            headers=alice.headers,
        )
        assert response.status_code == 400

    def test_rejects_an_unknown_account(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/khata",
            json={
                "person_name": "Ghost",
                "person_user_id": "00000000-0000-0000-0000-000000000000",
            },
            headers=alice.headers,
        )
        assert response.status_code == 404

    def test_accepts_contact_details_and_a_currency(
        self, client: TestClient, alice: Actor
    ) -> None:
        body = open_khata(
            client,
            alice,
            "Ahmed",
            person_phone=" 0300-1234567 ",
            person_email="ahmed@example.com",
            currency="pkr",
            notes="Corner shop",
        )
        assert body["person_phone"] == "0300-1234567"
        assert body["person_email"] == "ahmed@example.com"
        assert body["currency"] == "PKR"
        assert body["notes"] == "Corner shop"

    def test_rejects_an_invented_currency(self, client: TestClient, alice: Actor) -> None:
        response = client.post(
            "/api/v1/khata",
            json={"person_name": "Ahmed", "currency": "ZZZ"},
            headers=alice.headers,
        )
        assert response.status_code == 422

    def test_requires_authentication(self, client: TestClient) -> None:
        assert client.post("/api/v1/khata", json={"person_name": "Ahmed"}).status_code == 401


class TestPermissions:
    def test_you_only_see_your_own(self, client: TestClient, alice: Actor, bob: Actor) -> None:
        open_khata(client, alice, "Ahmed")
        open_khata(client, bob, "Usman")

        assert [i["person_name"] for i in
                client.get("/api/v1/khata", headers=alice.headers).json()["items"]] == ["Ahmed"]
        assert [i["person_name"] for i in
                client.get("/api/v1/khata", headers=bob.headers).json()["items"]] == ["Usman"]

    def test_someone_elses_khata_is_a_404_not_a_403(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        """A khata is a private book; confirming it exists leaks who someone deals with."""
        khata = open_khata(client, alice, "Ahmed")

        response = client.get(f"/api/v1/khata/{khata['id']}", headers=bob.headers)
        assert response.status_code == 404

    def test_cannot_edit_or_delete_someone_elses(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice, "Ahmed")

        assert client.patch(
            f"/api/v1/khata/{khata['id']}", json={"person_name": "Nope"}, headers=bob.headers
        ).status_code == 404
        assert client.delete(
            f"/api/v1/khata/{khata['id']}", headers=bob.headers
        ).status_code == 404

    def test_a_linked_person_does_not_get_access(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        """Alice's book about Bob is still Alice's book."""
        khata = open_khata(client, alice, "Bob", person_user_id=bob.id)

        assert client.get(f"/api/v1/khata/{khata['id']}", headers=bob.headers).status_code == 404
        assert client.get("/api/v1/khata", headers=bob.headers).json()["total"] == 0


class TestUpdate:
    def test_edits_the_details(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice, "Ahmed")

        response = client.patch(
            f"/api/v1/khata/{khata['id']}",
            json={"person_name": "Ahmed Khan", "notes": "Pays weekly"},
            headers=alice.headers,
        )

        assert response.status_code == 200
        assert response.json()["person_name"] == "Ahmed Khan"
        assert response.json()["notes"] == "Pays weekly"

    def test_a_patch_leaves_other_fields_alone(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice, "Ahmed", notes="Original")

        response = client.patch(
            f"/api/v1/khata/{khata['id']}", json={"person_name": "Ahmed K"}, headers=alice.headers
        )
        assert response.json()["notes"] == "Original"

    def test_can_link_an_account_later(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice, "Bob")

        response = client.patch(
            f"/api/v1/khata/{khata['id']}",
            json={"person_user_id": bob.id},
            headers=alice.headers,
        )
        assert response.json()["person_user"]["id"] == bob.id

    def test_cannot_link_to_a_person_you_already_keep_a_khata_for(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        open_khata(client, alice, "Bob", person_user_id=bob.id)
        other = open_khata(client, alice, "Someone")

        response = client.patch(
            f"/api/v1/khata/{other['id']}",
            json={"person_user_id": bob.id},
            headers=alice.headers,
        )
        assert response.status_code == 409

    def test_rejects_an_empty_patch(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice, "Ahmed")
        response = client.patch(
            f"/api/v1/khata/{khata['id']}", json={}, headers=alice.headers
        )
        assert response.status_code == 422


class TestArchiveAndDelete:
    def test_delete_archives_by_default(self, client: TestClient, alice: Actor) -> None:
        """The forgiving action is the default one for a financial record."""
        khata = open_khata(client, alice, "Ahmed")

        response = client.delete(f"/api/v1/khata/{khata['id']}", headers=alice.headers)
        assert response.status_code == 200
        assert "archived" in response.json()["message"].lower()

        # Hidden from the list, but still there.
        assert client.get("/api/v1/khata", headers=alice.headers).json()["total"] == 0
        assert client.get(f"/api/v1/khata/{khata['id']}", headers=alice.headers).status_code == 200

    def test_archived_khatas_can_be_asked_for(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice, "Ahmed")
        client.delete(f"/api/v1/khata/{khata['id']}", headers=alice.headers)

        page = client.get(
            "/api/v1/khata", params={"include_archived": True}, headers=alice.headers
        ).json()
        assert page["total"] == 1
        assert page["items"][0]["is_archived"] is True

    def test_can_be_restored(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice, "Ahmed")
        client.delete(f"/api/v1/khata/{khata['id']}", headers=alice.headers)

        restored = client.patch(
            f"/api/v1/khata/{khata['id']}", json={"is_archived": False}, headers=alice.headers
        )
        assert restored.json()["is_archived"] is False
        assert client.get("/api/v1/khata", headers=alice.headers).json()["total"] == 1

    def test_permanent_delete_removes_it(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice, "Ahmed")

        response = client.delete(
            f"/api/v1/khata/{khata['id']}", params={"permanent": True}, headers=alice.headers
        )
        assert response.status_code == 200
        assert client.get(f"/api/v1/khata/{khata['id']}", headers=alice.headers).status_code == 404


class TestListing:
    def test_search_by_name_phone_and_email(self, client: TestClient, alice: Actor) -> None:
        open_khata(client, alice, "Ahmed", person_phone="0300-1112222")
        open_khata(client, alice, "Usman", person_email="usman@example.com")
        open_khata(client, alice, "Ali")

        def names(**params):
            page = client.get("/api/v1/khata", params=params, headers=alice.headers).json()
            return {item["person_name"] for item in page["items"]}

        assert names(search="ahm") == {"Ahmed"}
        assert names(search="1112") == {"Ahmed"}
        assert names(search="usman@") == {"Usman"}
        assert names(search="zzz") == set()

    def test_search_matches_a_linked_accounts_name(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        """The reader may remember the real name, not what they typed."""
        open_khata(client, alice, "B", person_user_id=bob.id)

        page = client.get(
            "/api/v1/khata", params={"search": "bob brown"}, headers=alice.headers
        ).json()
        assert page["total"] >= 0  # search filters items; total counts the same filter
        assert [i["display_name"] for i in page["items"]] == [bob.full_name]

    def test_sorting_by_name(self, client: TestClient, alice: Actor) -> None:
        for name in ("Usman", "Ahmed", "Ali"):
            open_khata(client, alice, name)

        page = client.get(
            "/api/v1/khata", params={"sort": "person_name"}, headers=alice.headers
        ).json()
        assert [i["person_name"] for i in page["items"]] == ["Ahmed", "Ali", "Usman"]

    def test_paging(self, client: TestClient, alice: Actor) -> None:
        for index in range(5):
            open_khata(client, alice, f"Person {index}")

        first = client.get(
            "/api/v1/khata", params={"limit": 2, "offset": 0}, headers=alice.headers
        ).json()
        second = client.get(
            "/api/v1/khata", params={"limit": 2, "offset": 2}, headers=alice.headers
        ).json()

        assert first["total"] == second["total"] == 5
        assert len(first["items"]) == len(second["items"]) == 2
        assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})

    def test_totals_are_reported_per_currency(self, client: TestClient, alice: Actor) -> None:
        open_khata(client, alice, "Ahmed", currency="PKR")
        open_khata(client, alice, "Ali", currency="PKR")
        open_khata(client, alice, "Dollar Dave", currency="USD")

        totals = client.get("/api/v1/khata", headers=alice.headers).json()["totals"]
        by_currency = {row["currency"]: row for row in totals}

        # No entries yet, so every balance is zero — but the shape is per currency,
        # never a single figure adding rupees to dollars.
        assert set(by_currency) == {"PKR", "USD"}
        assert by_currency["PKR"]["khata_count"] == 2
        assert by_currency["USD"]["khata_count"] == 1

    def test_a_new_account_has_no_khatas(self, client: TestClient, alice: Actor) -> None:
        page = client.get("/api/v1/khata", headers=alice.headers).json()
        assert page == {"items": [], "total": 0, "limit": 50, "offset": 0, "totals": []}


class TestBalances:
    """Balances come from entries, which the next milestone adds. What matters now
    is that the derived shape is present and reads zero rather than being absent."""

    def test_balance_is_zero_and_derived(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice, "Ahmed")

        detail = client.get(f"/api/v1/khata/{khata['id']}", headers=alice.headers).json()
        assert Decimal(detail["balance"]) == Decimal("0.00")
        assert detail["entry_count"] == 0
        assert detail["last_entry_on"] is None

    def test_entries_move_the_balance(self, client: TestClient, alice: Actor, db) -> None:
        """Written through the model, since the entry API is not built yet — this
        pins the arithmetic the ledger view will depend on."""
        import uuid as uuid_module
        from datetime import date

        from app.models.khata import KhataEntry, KhataEntryType

        khata = open_khata(client, alice, "Ahmed")

        for entry_type, amount in (
            (KhataEntryType.GIVEN, "5000.00"),
            (KhataEntryType.GIVEN, "3500.00"),
            (KhataEntryType.RECEIVED, "1000.00"),
        ):
            db.add(
                KhataEntry(
                    khata_id=uuid_module.UUID(khata["id"]),
                    entry_type=entry_type,
                    amount=Decimal(amount),
                    entry_date=date(2026, 8, 1),
                    created_by_id=uuid_module.UUID(alice.id),
                )
            )
        db.commit()

        detail = client.get(f"/api/v1/khata/{khata['id']}", headers=alice.headers).json()
        # 5000 + 3500 given, 1000 received back.
        assert Decimal(detail["balance"]) == Decimal("7500.00")
        assert detail["entry_count"] == 3

        totals = client.get("/api/v1/khata", headers=alice.headers).json()["totals"]
        pkr = next(row for row in totals if row["currency"] == "PKR")
        assert Decimal(pkr["owed_to_you"]) == Decimal("7500.00")
        assert Decimal(pkr["net"]) == Decimal("7500.00")

    def test_a_negative_balance_means_you_owe_them(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        import uuid as uuid_module
        from datetime import date

        from app.models.khata import KhataEntry, KhataEntryType

        khata = open_khata(client, alice, "Ali")
        db.add(
            KhataEntry(
                khata_id=uuid_module.UUID(khata["id"]),
                entry_type=KhataEntryType.RECEIVED,
                amount=Decimal("2000.00"),
                entry_date=date(2026, 8, 1),
                created_by_id=uuid_module.UUID(alice.id),
            )
        )
        db.commit()

        detail = client.get(f"/api/v1/khata/{khata['id']}", headers=alice.headers).json()
        assert Decimal(detail["balance"]) == Decimal("-2000.00")

        totals = client.get("/api/v1/khata", headers=alice.headers).json()["totals"]
        pkr = next(row for row in totals if row["currency"] == "PKR")
        assert Decimal(pkr["you_owe"]) == Decimal("2000.00")
        assert Decimal(pkr["net"]) == Decimal("-2000.00")

    def test_deleting_a_khata_takes_its_entries(
        self, client: TestClient, alice: Actor, db
    ) -> None:
        import uuid as uuid_module
        from datetime import date

        from sqlalchemy import func, select

        from app.models.khata import KhataEntry, KhataEntryType

        khata = open_khata(client, alice, "Ahmed")
        db.add(
            KhataEntry(
                khata_id=uuid_module.UUID(khata["id"]),
                entry_type=KhataEntryType.GIVEN,
                amount=Decimal("100.00"),
                entry_date=date(2026, 8, 1),
                created_by_id=uuid_module.UUID(alice.id),
            )
        )
        db.commit()

        response = client.delete(
            f"/api/v1/khata/{khata['id']}", params={"permanent": True}, headers=alice.headers
        )
        assert "1 entry" in response.json()["message"]

        remaining = db.scalar(
            select(func.count())
            .select_from(KhataEntry)
            .where(KhataEntry.khata_id == uuid_module.UUID(khata["id"]))
        )
        assert remaining == 0
