"""Reports.

Two properties matter most and are asserted repeatedly: flows are bounded by the
window while positions are not, and every figure is scoped to a single currency.
"""

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from tests.conftest import Actor

TODAY = date.today()
FIRST_OF_MONTH = TODAY.replace(day=1)


def open_khata(client: TestClient, actor: Actor, name: str = "Ahmed", **extra) -> dict:
    response = client.post(
        "/api/v1/khata", json={"person_name": name, "currency": "PKR"} | extra,
        headers=actor.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def add_entry(client: TestClient, actor: Actor, khata_id: str, kind: str, amount: str, on=None):
    response = client.post(
        f"/api/v1/khata/{khata_id}/entries",
        json={"entry_type": kind, "amount": amount, "entry_date": (on or TODAY).isoformat()},
        headers=actor.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def give_loan(client: TestClient, actor: Actor, **extra) -> dict:
    body = {"counterparty_name": "Ahmed", "amount": "100000.00", "currency": "PKR"} | extra
    response = client.post("/api/v1/loans", json=body, headers=actor.headers)
    assert response.status_code == 201, response.text
    return response.json()


def pay(client: TestClient, actor: Actor, loan_id: str, amount: str, on=None):
    response = client.post(
        f"/api/v1/loans/{loan_id}/payments",
        json={"amount": amount, "payment_date": (on or TODAY).isoformat()},
        headers=actor.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def summary(client: TestClient, actor: Actor, **params) -> dict:
    response = client.get(
        "/api/v1/reports/summary", params={"currency": "PKR"} | params, headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestTheHeadlineFigures:
    def test_the_worked_example(self, client: TestClient, alice: Actor) -> None:
        """The shape from the brief: given, received, lent, repaid, receivable."""
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "150000.00")
        add_entry(client, alice, khata["id"], "received", "80000.00")

        loan = give_loan(client, alice)
        pay(client, alice, loan["id"], "30000.00")

        flow = summary(client, alice)["flow"]

        assert Decimal(flow["money_given"]) == Decimal("150000.00")
        assert Decimal(flow["money_received"]) == Decimal("80000.00")
        assert Decimal(flow["loans_given"]) == Decimal("100000.00")
        assert Decimal(flow["loan_payments"]) == Decimal("30000.00")
        # 150000 given - 80000 back = 70000 still receivable on the khata.
        assert Decimal(flow["khata_receivable"]) == Decimal("70000.00")
        assert Decimal(flow["loans_receivable"]) == Decimal("70000.00")
        assert Decimal(flow["loans_payable"]) == Decimal("0.00")

    def test_net_flow_adds_up(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "1000.00")
        add_entry(client, alice, khata["id"], "received", "400.00")
        loan = give_loan(client, alice, amount="500.00")
        pay(client, alice, loan["id"], "100.00")

        flow = summary(client, alice)["flow"]
        expected = (
            Decimal(flow["money_received"])
            + Decimal(flow["loan_payments"])
            + Decimal(flow["settlements_in"])
            - Decimal(flow["money_given"])
            - Decimal(flow["loans_given"])
            - Decimal(flow["settlements_out"])
        )
        assert Decimal(flow["net_flow"]) == expected

    def test_an_adjustment_falls_to_the_side_its_sign_puts_it(
        self, client: TestClient, alice: Actor
    ) -> None:
        """An adjustment corrects one of the two directions; it is not a third."""
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "adjustment", "300.00")
        add_entry(client, alice, khata["id"], "adjustment", "-100.00")

        flow = summary(client, alice)["flow"]
        assert Decimal(flow["money_given"]) == Decimal("300.00")
        assert Decimal(flow["money_received"]) == Decimal("100.00")


class TestFlowsAreWindowedButPositionsAreNot:
    def test_activity_outside_the_window_is_excluded(
        self, client: TestClient, alice: Actor
    ) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "5000.00", on=TODAY - timedelta(days=200))
        add_entry(client, alice, khata["id"], "given", "1000.00")

        flow = summary(
            client, alice,
            start_date=FIRST_OF_MONTH.isoformat(),
            end_date=TODAY.isoformat(),
        )["flow"]

        # Only this month's entry is a flow...
        assert Decimal(flow["money_given"]) == Decimal("1000.00")
        # ...but the receivable is a position, so it counts both.
        assert Decimal(flow["khata_receivable"]) == Decimal("6000.00")

    def test_a_window_with_no_activity_still_reports_the_position(
        self, client: TestClient, alice: Actor
    ) -> None:
        """A quiet month must not read as a settled book."""
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "9000.00", on=TODAY - timedelta(days=300))

        quiet = summary(
            client, alice,
            start_date=TODAY.isoformat(),
            end_date=TODAY.isoformat(),
        )["flow"]

        assert Decimal(quiet["money_given"]) == Decimal("0.00")
        assert Decimal(quiet["khata_receivable"]) == Decimal("9000.00")

    def test_the_window_defaults_to_this_month_and_is_labelled(
        self, client: TestClient, alice: Actor
    ) -> None:
        window = summary(client, alice)["window"]
        assert window["start_date"] == FIRST_OF_MONTH.isoformat()
        assert window["label"] == TODAY.strftime("%B %Y")

    def test_a_custom_window_is_labelled_with_both_ends(
        self, client: TestClient, alice: Actor
    ) -> None:
        window = summary(
            client, alice,
            start_date=(TODAY - timedelta(days=3)).isoformat(),
            end_date=TODAY.isoformat(),
        )["window"]
        assert " to " in window["label"]

    def test_a_backwards_window_is_refused(self, client: TestClient, alice: Actor) -> None:
        response = client.get(
            "/api/v1/reports/summary",
            params={"start_date": TODAY.isoformat(), "end_date": (TODAY - timedelta(days=1)).isoformat()},
            headers=alice.headers,
        )
        assert response.status_code == 400

    def test_an_absurd_window_is_refused(self, client: TestClient, alice: Actor) -> None:
        response = client.get(
            "/api/v1/reports/summary",
            params={"start_date": "1990-01-01", "end_date": TODAY.isoformat()},
            headers=alice.headers,
        )
        assert response.status_code == 400


class TestCurrencyScoping:
    def test_another_currency_is_not_added_in(self, client: TestClient, alice: Actor) -> None:
        """Adding rupees to dollars would produce a number with no meaning."""
        pkr = open_khata(client, alice, name="Ahmed", currency="PKR")
        usd = open_khata(client, alice, name="Bilal", currency="USD")
        add_entry(client, alice, pkr["id"], "given", "5000.00")
        add_entry(client, alice, usd["id"], "given", "100.00")

        in_pkr = summary(client, alice, currency="PKR")["flow"]
        in_usd = summary(client, alice, currency="USD")["flow"]

        assert Decimal(in_pkr["money_given"]) == Decimal("5000.00")
        assert Decimal(in_usd["money_given"]) == Decimal("100.00")


class TestKhataReport:
    def test_a_row_per_khata_with_window_activity_and_standing_balance(
        self, client: TestClient, alice: Actor
    ) -> None:
        ahmed = open_khata(client, alice, name="Ahmed")
        ali = open_khata(client, alice, name="Ali")

        add_entry(client, alice, ahmed["id"], "given", "5000.00", on=TODAY - timedelta(days=200))
        add_entry(client, alice, ahmed["id"], "given", "2000.00")
        add_entry(client, alice, ali["id"], "given", "1000.00")
        add_entry(client, alice, ali["id"], "received", "400.00")

        report = client.get(
            "/api/v1/reports/khata",
            params={"currency": "PKR", "start_date": FIRST_OF_MONTH.isoformat(), "end_date": TODAY.isoformat()},
            headers=alice.headers,
        ).json()

        rows = {row["name"]: row for row in report["rows"]}
        assert Decimal(rows["Ahmed"]["given"]) == Decimal("2000.00")  # windowed
        assert Decimal(rows["Ahmed"]["balance"]) == Decimal("7000.00")  # standing
        assert Decimal(rows["Ali"]["received"]) == Decimal("400.00")
        assert Decimal(rows["Ali"]["balance"]) == Decimal("600.00")

        assert Decimal(report["totals"]["balance"]) == Decimal("7600.00")

    def test_a_khata_with_no_entries_still_appears(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Omitting it would make an empty khata indistinguishable from none."""
        open_khata(client, alice, name="Quiet")
        report = client.get(
            "/api/v1/reports/khata", params={"currency": "PKR"}, headers=alice.headers
        ).json()

        assert [row["name"] for row in report["rows"]] == ["Quiet"]
        assert Decimal(report["rows"][0]["balance"]) == Decimal("0.00")

    def test_narrowing_to_one_person(
        self, client: TestClient, alice: Actor, bob: Actor, befriend
    ) -> None:
        befriend(alice, bob)
        linked = open_khata(client, alice, name=bob.full_name, person_user_id=bob.id)
        open_khata(client, alice, name="Someone else")
        add_entry(client, alice, linked["id"], "given", "700.00")

        report = client.get(
            "/api/v1/reports/khata",
            params={"currency": "PKR", "person_user_id": bob.id},
            headers=alice.headers,
        ).json()

        assert len(report["rows"]) == 1
        assert Decimal(report["totals"]["given"]) == Decimal("700.00")


class TestLoanReport:
    def test_rows_carry_both_standing_and_windowed_figures(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice, amount="50000.00")
        pay(client, alice, loan["id"], "10000.00", on=TODAY - timedelta(days=200))
        pay(client, alice, loan["id"], "5000.00")

        report = client.get(
            "/api/v1/reports/loans",
            params={"currency": "PKR", "start_date": FIRST_OF_MONTH.isoformat(), "end_date": TODAY.isoformat()},
            headers=alice.headers,
        ).json()

        row = report["rows"][0]
        assert Decimal(row["paid"]) == Decimal("15000.00")  # standing
        assert Decimal(row["paid_in_window"]) == Decimal("5000.00")  # windowed
        assert Decimal(row["remaining"]) == Decimal("35000.00")
        assert row["status"] == "partially_paid"

        assert Decimal(report["totals"]["repaid_in_window"]) == Decimal("5000.00")
        assert Decimal(report["totals"]["receivable"]) == Decimal("35000.00")

    def test_cancelled_loans_are_excluded_unless_asked_for(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice)
        client.delete(f"/api/v1/loans/{loan['id']}", headers=alice.headers)

        without = client.get(
            "/api/v1/reports/loans", params={"currency": "PKR"}, headers=alice.headers
        ).json()
        assert without["rows"] == []

        with_them = client.get(
            "/api/v1/reports/loans",
            params={"currency": "PKR", "include_cancelled": True},
            headers=alice.headers,
        ).json()
        assert len(with_them["rows"]) == 1
        assert with_them["rows"][0]["status"] == "cancelled"

    def test_overdue_is_totalled(self, client: TestClient, alice: Actor) -> None:
        give_loan(client, alice, amount="8000.00", due_date=(TODAY - timedelta(days=5)).isoformat())
        report = client.get(
            "/api/v1/reports/loans", params={"currency": "PKR"}, headers=alice.headers
        ).json()
        assert Decimal(report["totals"]["overdue"]) == Decimal("8000.00")

    def test_the_report_agrees_with_the_loan_itself(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice, amount="9000.00")
        pay(client, alice, loan["id"], "2500.00")

        detail = client.get(f"/api/v1/loans/{loan['id']}", headers=alice.headers).json()
        report = client.get(
            "/api/v1/reports/loans", params={"currency": "PKR"}, headers=alice.headers
        ).json()

        assert Decimal(report["rows"][0]["remaining"]) == Decimal(detail["remaining"])
        assert report["rows"][0]["status"] == detail["status"]


class TestActivitySeries:
    def test_empty_periods_come_back_as_zero(self, client: TestClient, alice: Actor) -> None:
        """A series that skips quiet months misrepresents the shape."""
        response = client.get(
            "/api/v1/reports/activity",
            params={
                "currency": "PKR",
                "granularity": "monthly",
                "start_date": (FIRST_OF_MONTH - timedelta(days=120)).isoformat(),
                "end_date": TODAY.isoformat(),
            },
            headers=alice.headers,
        )
        assert response.status_code == 200, response.text
        points = response.json()["points"]

        assert len(points) >= 4
        assert all(Decimal(point["given"]) == Decimal("0.00") for point in points)
        # The axis is even: consecutive months, none skipped.
        periods = [point["period"] for point in points]
        assert periods == sorted(periods)

    def test_activity_lands_in_the_right_bucket(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "1500.00")
        add_entry(client, alice, khata["id"], "received", "500.00")
        loan = give_loan(client, alice, amount="4000.00")
        pay(client, alice, loan["id"], "1000.00")

        points = client.get(
            "/api/v1/reports/activity",
            params={"currency": "PKR", "granularity": "monthly"},
            headers=alice.headers,
        ).json()["points"]

        this_month = next(p for p in points if p["period"] == TODAY.strftime("%Y-%m"))
        assert Decimal(this_month["given"]) == Decimal("1500.00")
        assert Decimal(this_month["received"]) == Decimal("500.00")
        assert Decimal(this_month["lent"]) == Decimal("4000.00")
        assert Decimal(this_month["repaid"]) == Decimal("1000.00")

    def test_daily_granularity(self, client: TestClient, alice: Actor) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "200.00")

        points = client.get(
            "/api/v1/reports/activity",
            params={
                "currency": "PKR",
                "granularity": "daily",
                "start_date": (TODAY - timedelta(days=3)).isoformat(),
                "end_date": TODAY.isoformat(),
            },
            headers=alice.headers,
        ).json()["points"]

        assert len(points) == 4
        assert points[-1]["period"] == TODAY.isoformat()
        assert Decimal(points[-1]["given"]) == Decimal("200.00")


class TestPermissions:
    def test_you_only_ever_see_your_own_money(
        self, client: TestClient, alice: Actor, bob: Actor
    ) -> None:
        khata = open_khata(client, alice)
        add_entry(client, alice, khata["id"], "given", "5000.00")

        hers = summary(client, alice)["flow"]
        his = summary(client, bob)["flow"]

        assert Decimal(hers["money_given"]) == Decimal("5000.00")
        assert Decimal(his["money_given"]) == Decimal("0.00")
        assert Decimal(his["khata_receivable"]) == Decimal("0.00")

    def test_reports_need_a_token(self, client: TestClient) -> None:
        for path in ("summary", "khata", "loans", "activity"):
            assert client.get(f"/api/v1/reports/{path}").status_code == 401


class TestLoanDirectionInReports:
    def test_given_and_taken_are_reported_separately(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Netting them would hide that money runs both ways."""
        give_loan(client, alice, amount="50000.00")
        give_loan(client, alice, amount="30000.00", direction="taken")

        flow = summary(client, alice)["flow"]
        assert Decimal(flow["loans_given"]) == Decimal("50000.00")
        assert Decimal(flow["loans_taken"]) == Decimal("30000.00")
        assert Decimal(flow["loans_receivable"]) == Decimal("50000.00")
        assert Decimal(flow["loans_payable"]) == Decimal("30000.00")

    def test_repayments_are_split_by_direction(self, client: TestClient, alice: Actor) -> None:
        given = give_loan(client, alice, amount="5000.00")
        taken = give_loan(client, alice, amount="4000.00", direction="taken")
        pay(client, alice, given["id"], "1000.00")
        pay(client, alice, taken["id"], "700.00")

        flow = summary(client, alice)["flow"]
        assert Decimal(flow["loan_payments"]) == Decimal("1000.00")
        assert Decimal(flow["loan_repayments_made"]) == Decimal("700.00")

    def test_borrowing_counts_as_money_in_and_repaying_as_money_out(
        self, client: TestClient, alice: Actor
    ) -> None:
        taken = give_loan(client, alice, amount="1000.00", direction="taken")
        pay(client, alice, taken["id"], "400.00")

        flow = summary(client, alice)["flow"]
        # +1000 borrowed, -400 repaid.
        assert Decimal(flow["net_flow"]) == Decimal("600.00")

    def test_an_overpayment_becomes_money_you_owe(
        self, client: TestClient, alice: Actor
    ) -> None:
        loan = give_loan(client, alice, amount="1000.00")
        pay(client, alice, loan["id"], "1500.00")

        flow = summary(client, alice)["flow"]
        assert Decimal(flow["loans_receivable"]) == Decimal("0.00")
        assert Decimal(flow["loans_payable"]) == Decimal("500.00")

    def test_the_loan_report_carries_the_direction(
        self, client: TestClient, alice: Actor
    ) -> None:
        give_loan(client, alice, counterparty_name="Lent to", amount="900.00")
        give_loan(client, alice, counterparty_name="Borrowed from", amount="200.00", direction="taken")

        report = client.get(
            "/api/v1/reports/loans", params={"currency": "PKR"}, headers=alice.headers
        ).json()
        by_name = {row["counterparty_name"]: row for row in report["rows"]}

        assert by_name["Lent to"]["direction"] == "given"
        assert by_name["Borrowed from"]["direction"] == "taken"
        assert Decimal(by_name["Borrowed from"]["signed_balance"]) == Decimal("-200.00")

        assert Decimal(report["totals"]["lent"]) == Decimal("900.00")
        assert Decimal(report["totals"]["borrowed"]) == Decimal("200.00")
        assert Decimal(report["totals"]["net"]) == Decimal("700.00")

    def test_the_activity_chart_follows_loans_you_gave(
        self, client: TestClient, alice: Actor
    ) -> None:
        """Mixing in loans you took would invert the meaning of both series."""
        give_loan(client, alice, amount="5000.00")
        give_loan(client, alice, amount="9000.00", direction="taken")

        points = client.get(
            "/api/v1/reports/activity",
            params={"currency": "PKR", "granularity": "monthly"},
            headers=alice.headers,
        ).json()["points"]

        this_month = next(p for p in points if p["period"] == TODAY.strftime("%Y-%m"))
        assert Decimal(this_month["lent"]) == Decimal("5000.00")
