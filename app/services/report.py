"""Reports: the money in and out over a window, per currency.

Three reports share one shape — a date range, an optional person, and totals that
add up. They read from the same rows every other page reads from; nothing here is
precomputed or cached, so a report can never describe a state the app is no longer
in.

**Everything is scoped to one currency.** A report that added rupees to dollars
would produce a number with no meaning, and picking a rate to convert them is a
decision this app has no business making silently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app.models.expense import Expense, ExpenseSplit
from app.models.khata import KhataAccount, KhataEntry, KhataEntryType
from app.models.loan import Loan, LoanDirection, LoanPayment
from app.models.settlement import Settlement
from app.models.user import User

ZERO = Decimal("0.00")


def _q(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class MoneyFlow:
    """The headline figures from the brief, for one currency and one window."""

    currency: str
    start_date: date
    end_date: date

    money_given: Decimal
    """Khata `given` plus `adjustment` increases — money that left your hands."""
    money_received: Decimal
    """Khata `received` plus `adjustment` decreases — money that came back."""

    loans_given: Decimal
    """Principal you lent out in the window."""
    loans_taken: Decimal
    """Principal you borrowed in the window."""

    loan_payments: Decimal
    """Repayments that came back to you, on loans you gave."""
    loan_repayments_made: Decimal
    """Repayments you made, on loans you took."""

    khata_receivable: Decimal
    """Outstanding across all khatas *as of now*, not just this window.

    A window figure would be misleading here: what you are owed is a position, not
    a flow, and it does not reset because a month ended.
    """
    loans_receivable: Decimal
    """Owed to you across all open loans, as of now."""
    loans_payable: Decimal
    """Owed by you across all open loans, overpayments included."""

    expenses_paid: Decimal
    """Your share of group expenses in the window."""
    settlements_in: Decimal
    settlements_out: Decimal

    net_flow: Decimal
    """Received plus repayments, less given and lent. Positive means money came in."""


def money_flow(
    db: Session,
    viewer: User,
    *,
    start_date: date,
    end_date: date,
    currency: str,
    person_user_id: uuid.UUID | None = None,
) -> MoneyFlow:
    khata_filter = [
        KhataAccount.owner_id == viewer.id,
        KhataAccount.currency == currency,
    ]
    if person_user_id is not None:
        khata_filter.append(KhataAccount.person_user_id == person_user_id)

    khata_ids = select(KhataAccount.id).where(*khata_filter)

    # Given and received are split by type, with adjustments falling to whichever
    # side their sign puts them on — an adjustment is a correction to one of the
    # two, not a third kind of movement.
    given_expr = case(
        (KhataEntry.entry_type == KhataEntryType.GIVEN.value, KhataEntry.amount),
        (
            (KhataEntry.entry_type == KhataEntryType.ADJUSTMENT.value)
            & (KhataEntry.amount > 0),
            KhataEntry.amount,
        ),
        else_=0,
    )
    received_expr = case(
        (KhataEntry.entry_type == KhataEntryType.RECEIVED.value, KhataEntry.amount),
        (
            (KhataEntry.entry_type == KhataEntryType.ADJUSTMENT.value)
            & (KhataEntry.amount < 0),
            -KhataEntry.amount,
        ),
        else_=0,
    )

    khata_window = [
        KhataEntry.khata_id.in_(khata_ids),
        KhataEntry.entry_date >= start_date,
        KhataEntry.entry_date <= end_date,
    ]

    money_given, money_received = db.execute(
        select(
            func.coalesce(func.sum(given_expr), 0),
            func.coalesce(func.sum(received_expr), 0),
        ).where(*khata_window)
    ).one()

    # The receivable is a position as of now, so it deliberately ignores the window.
    khata_receivable = db.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (KhataEntry.entry_type == KhataEntryType.GIVEN.value, KhataEntry.amount),
                        (
                            KhataEntry.entry_type == KhataEntryType.RECEIVED.value,
                            -KhataEntry.amount,
                        ),
                        else_=KhataEntry.amount,
                    )
                ),
                0,
            )
        ).where(KhataEntry.khata_id.in_(khata_ids))
    )

    loan_filter = [Loan.owner_id == viewer.id, Loan.currency == currency]
    if person_user_id is not None:
        loan_filter.append(Loan.counterparty_user_id == person_user_id)

    def _principal_in_window(direction: LoanDirection) -> Decimal:
        return _q(
            db.scalar(
                select(func.coalesce(func.sum(Loan.amount), 0)).where(
                    *loan_filter,
                    Loan.direction == direction,
                    Loan.cancelled_at.is_(None),
                    func.date(Loan.created_at) >= start_date,
                    func.date(Loan.created_at) <= end_date,
                )
            )
        )

    loans_given = _principal_in_window(LoanDirection.GIVEN)
    loans_taken = _principal_in_window(LoanDirection.TAKEN)

    def _payments_in_window(direction: LoanDirection) -> Decimal:
        ids = select(Loan.id).where(
            *loan_filter, Loan.direction == direction, Loan.cancelled_at.is_(None)
        )
        return _q(
            db.scalar(
                select(func.coalesce(func.sum(LoanPayment.amount), 0)).where(
                    LoanPayment.loan_id.in_(ids),
                    LoanPayment.payment_date >= start_date,
                    LoanPayment.payment_date <= end_date,
                )
            )
        )

    loan_payments = _payments_in_window(LoanDirection.GIVEN)
    loan_repayments_made = _payments_in_window(LoanDirection.TAKEN)

    # The standing positions need each loan's payments, so they are computed in
    # Python from the models rather than as a second aggregate that could disagree
    # with `signed_balance`. Reported as two sides rather than one net figure:
    # being owed 50,000 while owing 30,000 is not the same as being owed 20,000.
    open_loans = db.scalars(
        select(Loan).options(selectinload(Loan.payments)).where(*loan_filter, Loan.cancelled_at.is_(None))
    ).all()
    loans_receivable = sum(
        (loan.signed_balance for loan in open_loans if loan.signed_balance > 0), ZERO
    )
    loans_payable = sum(
        (-loan.signed_balance for loan in open_loans if loan.signed_balance < 0), ZERO
    )

    expense_conditions = [
        ExpenseSplit.user_id == viewer.id,
        Expense.currency == currency,
        Expense.expense_date >= start_date,
        Expense.expense_date <= end_date,
    ]
    if person_user_id is not None:
        expense_conditions.append(
            Expense.id.in_(
                select(ExpenseSplit.expense_id).where(ExpenseSplit.user_id == person_user_id)
            )
        )

    expenses_paid = db.scalar(
        select(func.coalesce(func.sum(ExpenseSplit.amount), 0))
        .select_from(ExpenseSplit)
        .join(Expense, Expense.id == ExpenseSplit.expense_id)
        .where(*expense_conditions)
    )

    settlement_window = [
        Settlement.currency == currency,
        Settlement.settled_on >= start_date,
        Settlement.settled_on <= end_date,
    ]

    in_conditions = [*settlement_window, Settlement.to_user_id == viewer.id]
    out_conditions = [*settlement_window, Settlement.from_user_id == viewer.id]
    if person_user_id is not None:
        in_conditions.append(Settlement.from_user_id == person_user_id)
        out_conditions.append(Settlement.to_user_id == person_user_id)

    settlements_in = db.scalar(
        select(func.coalesce(func.sum(Settlement.amount), 0)).where(*in_conditions)
    )
    settlements_out = db.scalar(
        select(func.coalesce(func.sum(Settlement.amount), 0)).where(*out_conditions)
    )

    given = _q(money_given)
    received = _q(money_received)
    lent = loans_given
    borrowed = loans_taken
    repaid = loan_payments
    repaid_out = loan_repayments_made
    in_ = _q(settlements_in)
    out = _q(settlements_out)

    return MoneyFlow(
        currency=currency,
        start_date=start_date,
        end_date=end_date,
        money_given=given,
        money_received=received,
        loans_given=lent,
        loans_taken=borrowed,
        loan_payments=repaid,
        loan_repayments_made=repaid_out,
        khata_receivable=_q(khata_receivable),
        loans_receivable=_q(loans_receivable),
        loans_payable=_q(loans_payable),
        expenses_paid=_q(expenses_paid),
        settlements_in=in_,
        settlements_out=out,
        # Money in, less money out. Borrowing is money in; repaying it is money out.
        net_flow=(received + repaid + in_ + borrowed) - (given + lent + out + repaid_out),
    )


@dataclass(frozen=True, slots=True)
class PersonRow:
    """One counterparty's line in a report."""

    person_user_id: uuid.UUID | None
    name: str
    khata_id: uuid.UUID | None = None
    given: Decimal = ZERO
    received: Decimal = ZERO
    balance: Decimal = ZERO
    entry_count: int = 0


def khata_report(
    db: Session,
    viewer: User,
    *,
    start_date: date,
    end_date: date,
    currency: str,
    person_user_id: uuid.UUID | None = None,
) -> tuple[list[PersonRow], dict[str, Decimal]]:
    """Per khata: given and received in the window, and the balance as it stands.

    The two answer different questions — activity over a period, and what is owed
    right now — and a report that mixed them would let a quiet month read as a
    settled book.
    """
    conditions = [KhataAccount.owner_id == viewer.id, KhataAccount.currency == currency]
    if person_user_id is not None:
        conditions.append(KhataAccount.person_user_id == person_user_id)

    khatas = list(
        db.scalars(select(KhataAccount).where(*conditions).order_by(KhataAccount.person_name))
    )
    if not khatas:
        return [], {"given": ZERO, "received": ZERO, "balance": ZERO}

    given_expr = case(
        (KhataEntry.entry_type == KhataEntryType.GIVEN.value, KhataEntry.amount),
        ((KhataEntry.entry_type == KhataEntryType.ADJUSTMENT.value) & (KhataEntry.amount > 0), KhataEntry.amount),
        else_=0,
    )
    received_expr = case(
        (KhataEntry.entry_type == KhataEntryType.RECEIVED.value, KhataEntry.amount),
        ((KhataEntry.entry_type == KhataEntryType.ADJUSTMENT.value) & (KhataEntry.amount < 0), -KhataEntry.amount),
        else_=0,
    )
    signed_expr = case(
        (KhataEntry.entry_type == KhataEntryType.GIVEN.value, KhataEntry.amount),
        (KhataEntry.entry_type == KhataEntryType.RECEIVED.value, -KhataEntry.amount),
        else_=KhataEntry.amount,
    )

    ids = [khata.id for khata in khatas]

    # One query for the windowed activity, one for the standing balances. Two
    # queries rather than N per khata.
    windowed = {
        row.khata_id: row
        for row in db.execute(
            select(
                KhataEntry.khata_id.label("khata_id"),
                func.coalesce(func.sum(given_expr), 0).label("given"),
                func.coalesce(func.sum(received_expr), 0).label("received"),
                func.count().label("entry_count"),
            )
            .where(
                KhataEntry.khata_id.in_(ids),
                KhataEntry.entry_date >= start_date,
                KhataEntry.entry_date <= end_date,
            )
            .group_by(KhataEntry.khata_id)
        ).all()
    }

    balances = {
        khata_id: balance
        for khata_id, balance in db.execute(
            select(KhataEntry.khata_id, func.coalesce(func.sum(signed_expr), 0))
            .where(KhataEntry.khata_id.in_(ids))
            .group_by(KhataEntry.khata_id)
        ).all()
    }

    rows = [
        PersonRow(
            person_user_id=khata.person_user_id,
            name=khata.display_name,
            khata_id=khata.id,
            given=_q(windowed[khata.id].given if khata.id in windowed else 0),
            received=_q(windowed[khata.id].received if khata.id in windowed else 0),
            balance=_q(balances.get(khata.id, 0)),
            entry_count=windowed[khata.id].entry_count if khata.id in windowed else 0,
        )
        for khata in khatas
    ]

    totals = {
        "given": _q(sum((row.given for row in rows), ZERO)),
        "received": _q(sum((row.received for row in rows), ZERO)),
        "balance": _q(sum((row.balance for row in rows), ZERO)),
    }
    return rows, totals


@dataclass(frozen=True, slots=True)
class LoanReportRow:
    loan_id: uuid.UUID
    counterparty_name: str
    direction: str
    person_user_id: uuid.UUID | None
    amount: Decimal
    paid: Decimal
    remaining: Decimal
    overpaid: Decimal
    signed_balance: Decimal
    status: str
    due_date: date | None
    paid_in_window: Decimal
    given_in_window: bool


def loan_report(
    db: Session,
    viewer: User,
    *,
    start_date: date,
    end_date: date,
    currency: str,
    person_user_id: uuid.UUID | None = None,
    include_cancelled: bool = False,
) -> tuple[list[LoanReportRow], dict[str, Decimal]]:
    conditions = [Loan.owner_id == viewer.id, Loan.currency == currency]
    if person_user_id is not None:
        conditions.append(Loan.counterparty_user_id == person_user_id)
    if not include_cancelled:
        conditions.append(Loan.cancelled_at.is_(None))

    loans = list(
        db.scalars(
            select(Loan)
            .options(selectinload(Loan.payments))
            .where(*conditions)
            .order_by(Loan.due_date.is_(None), Loan.due_date, Loan.created_at.desc())
        )
    )

    rows = []
    for loan in loans:
        paid_in_window = sum(
            (
                payment.amount
                for payment in loan.payments
                if start_date <= payment.payment_date <= end_date
            ),
            ZERO,
        )
        rows.append(
            LoanReportRow(
                loan_id=loan.id,
                counterparty_name=loan.display_name,
                direction=loan.direction.value,
                person_user_id=loan.counterparty_user_id,
                amount=loan.amount,
                paid=_q(loan.paid),
                remaining=_q(loan.remaining),
                overpaid=_q(loan.overpaid),
                signed_balance=_q(loan.signed_balance),
                status=loan.status.value,
                due_date=loan.due_date,
                paid_in_window=_q(paid_in_window),
                given_in_window=start_date <= loan.created_at.date() <= end_date,
            )
        )

    given_rows = [row for row in rows if row.direction == LoanDirection.GIVEN.value]
    taken_rows = [row for row in rows if row.direction == LoanDirection.TAKEN.value]

    totals = {
        "lent": _q(sum((row.amount for row in given_rows), ZERO)),
        "borrowed": _q(sum((row.amount for row in taken_rows), ZERO)),
        "repaid": _q(sum((row.paid for row in rows), ZERO)),
        "receivable": _q(sum((row.signed_balance for row in rows if row.signed_balance > 0), ZERO)),
        "payable": _q(sum((-row.signed_balance for row in rows if row.signed_balance < 0), ZERO)),
        "net": _q(sum((row.signed_balance for row in rows), ZERO)),
        "repaid_in_window": _q(sum((row.paid_in_window for row in rows), ZERO)),
        "lent_in_window": _q(
            sum((row.amount for row in given_rows if row.given_in_window), ZERO)
        ),
        "borrowed_in_window": _q(
            sum((row.amount for row in taken_rows if row.given_in_window), ZERO)
        ),
        "overdue": _q(sum((row.remaining for row in rows if row.status == "overdue"), ZERO)),
    }
    return rows, totals


@dataclass(frozen=True, slots=True)
class ActivityBucket:
    """One period on the activity chart."""

    period: str
    given: Decimal = ZERO
    received: Decimal = ZERO
    lent: Decimal = ZERO
    repaid: Decimal = ZERO


def activity_series(
    db: Session,
    viewer: User,
    *,
    start_date: date,
    end_date: date,
    currency: str,
    granularity: str = "monthly",
) -> list[ActivityBucket]:
    """Money in and out per period, with empty periods returned as zero.

    Gaps are filled rather than omitted so a chart gets an even time axis: a series
    that skips quiet months compresses them and misrepresents the shape.
    """
    truncate = "month" if granularity == "monthly" else "day"

    khata_ids = select(KhataAccount.id).where(
        KhataAccount.owner_id == viewer.id, KhataAccount.currency == currency
    )
    # The chart plots money you lent against money that came back, so it follows
    # loans you gave. Loans you took are a different story and would invert the
    # meaning of both series if they were mixed in.
    loan_ids = select(Loan.id).where(
        Loan.owner_id == viewer.id,
        Loan.currency == currency,
        Loan.direction == LoanDirection.GIVEN,
        Loan.cancelled_at.is_(None),
    )

    buckets: dict[str, dict[str, Decimal]] = {}

    def bucket_for(day: date) -> dict[str, Decimal]:
        key = day.strftime("%Y-%m") if truncate == "month" else day.isoformat()
        return buckets.setdefault(
            key, {"given": ZERO, "received": ZERO, "lent": ZERO, "repaid": ZERO}
        )

    for entry_type, amount, entry_date in db.execute(
        select(KhataEntry.entry_type, KhataEntry.amount, KhataEntry.entry_date).where(
            KhataEntry.khata_id.in_(khata_ids),
            KhataEntry.entry_date >= start_date,
            KhataEntry.entry_date <= end_date,
        )
    ).all():
        bucket = bucket_for(entry_date)
        kind = entry_type.value if hasattr(entry_type, "value") else str(entry_type)
        if kind == KhataEntryType.GIVEN.value or (kind == KhataEntryType.ADJUSTMENT.value and amount > 0):
            bucket["given"] += amount
        else:
            bucket["received"] += abs(amount)

    for amount, created_at in db.execute(
        select(Loan.amount, Loan.created_at).where(
            Loan.id.in_(loan_ids),
            func.date(Loan.created_at) >= start_date,
            func.date(Loan.created_at) <= end_date,
        )
    ).all():
        bucket_for(created_at.date())["lent"] += amount

    for amount, payment_date in db.execute(
        select(LoanPayment.amount, LoanPayment.payment_date).where(
            LoanPayment.loan_id.in_(loan_ids),
            LoanPayment.payment_date >= start_date,
            LoanPayment.payment_date <= end_date,
        )
    ).all():
        bucket_for(payment_date)["repaid"] += amount

    # Fill the axis.
    periods: list[str] = []
    if truncate == "month":
        year, month = start_date.year, start_date.month
        while (year, month) <= (end_date.year, end_date.month):
            periods.append(f"{year:04d}-{month:02d}")
            month += 1
            if month > 12:
                year, month = year + 1, 1
    else:
        day = start_date
        while day <= end_date:
            periods.append(day.isoformat())
            day = date.fromordinal(day.toordinal() + 1)

    return [
        ActivityBucket(
            period=period,
            **{key: _q(value) for key, value in buckets.get(period, {}).items()},
        )
        if period in buckets
        else ActivityBucket(period=period)
        for period in periods
    ]


def available_currencies(db: Session, viewer: User) -> list[str]:
    """Currencies this person actually has money recorded in, most active first.

    Every report is scoped to one currency, and defaulting to the profile currency
    alone means someone whose khatas are all in rupees while their profile says
    dollars opens Reports to a page of zeroes. That looks like a broken report
    rather than a currency mismatch, so the page is told what it could show.
    """
    counts: dict[str, int] = {}

    for currency, count in db.execute(
        select(KhataAccount.currency, func.count())
        .join(KhataEntry, KhataEntry.khata_id == KhataAccount.id)
        .where(KhataAccount.owner_id == viewer.id)
        .group_by(KhataAccount.currency)
    ).all():
        counts[currency] = counts.get(currency, 0) + count

    for currency, count in db.execute(
        select(Loan.currency, func.count())
        .where(Loan.owner_id == viewer.id)
        .group_by(Loan.currency)
    ).all():
        counts[currency] = counts.get(currency, 0) + count

    for currency, count in db.execute(
        select(Expense.currency, func.count())
        .join(ExpenseSplit, ExpenseSplit.expense_id == Expense.id)
        .where(ExpenseSplit.user_id == viewer.id)
        .group_by(Expense.currency)
    ).all():
        counts[currency] = counts.get(currency, 0) + count

    # Only currencies with something actually in them. The viewer's own currency is
    # deliberately *not* added at zero: a caller uses this list to decide whether
    # the currency it was going to show has anything to show, and padding it with a
    # guaranteed member would make that question unanswerable. Every currency stays
    # selectable in the UI regardless — this is about picking a sensible default.
    return [
        code
        for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if count > 0
    ]
