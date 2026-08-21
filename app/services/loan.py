"""Loans: creating them, paying them down, and working out where they stand.

Payments are the source of truth. `paid`, `remaining`, `overpaid`, `signed_balance`
and `status` are all computed from them, so a payment that is added, edited or
removed can never leave a loan whose headline figures disagree with its own history.

A loan runs in one of two directions — one you gave or one you took — and every
figure that leaves this module is signed the same way as the rest of the app:
**positive means they owe you**.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableEntityError,
)
from app.models.loan import Loan, LoanDirection, LoanPayment, LoanStatus
from app.models.user import User
from app.schemas.loan import LoanCreate, LoanPaymentCreate, LoanPaymentUpdate, LoanUpdate

ZERO = Decimal("0.00")


def _paid_subquery():
    """Total repaid per loan, as a joinable subquery.

    Aggregated in a subquery rather than with a GROUP BY on the outer select: the
    outer select eager-loads the counterparty, and every one of its columns would then
    have to appear in the grouping.
    """
    return (
        select(
            LoanPayment.loan_id.label("loan_id"),
            func.coalesce(func.sum(LoanPayment.amount), 0).label("paid"),
        )
        .group_by(LoanPayment.loan_id)
        .subquery()
    )


def get_or_404(db: Session, loan_id: uuid.UUID, owner_id: uuid.UUID) -> Loan:
    """Owner-scoped in the query, so someone else's loan is a 404 rather than a 403.

    Confirming that a loan exists but is not yours would leak who a person deals with.
    """
    loan = db.scalar(
        select(Loan)
        .options(selectinload(Loan.payments))
        .where(Loan.id == loan_id, Loan.owner_id == owner_id)
    )
    if loan is None:
        raise NotFoundError("Loan not found.")
    return loan


def _matches_status(loan: Loan, wanted: LoanStatus) -> bool:
    return loan.status is wanted


def list_for_owner(
    db: Session,
    owner_id: uuid.UUID,
    *,
    status: LoanStatus | None = None,
    direction: LoanDirection | None = None,
    counterparty_user_id: uuid.UUID | None = None,
    search: str | None = None,
    currency: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Loan], int]:
    """Your loans, soonest-due first.

    Status is filtered in Python, not SQL. Three of the five statuses depend on the
    payment total and today's date, so expressing them as SQL predicates would mean
    restating the whole `Loan.status` rule in a second language — the exact
    duplication that lets two answers drift apart. One person's loan list is small
    enough that loading it and filtering is the cheaper mistake to avoid.
    """
    conditions = [Loan.owner_id == owner_id]

    if direction is not None:
        conditions.append(Loan.direction == direction)
    if counterparty_user_id is not None:
        conditions.append(Loan.counterparty_user_id == counterparty_user_id)
    if currency is not None:
        conditions.append(Loan.currency == currency)
    if search:
        term = f"%{search.strip().lower()}%"
        conditions.append(
            or_(
                func.lower(Loan.counterparty_name).like(term),
                func.lower(func.coalesce(Loan.description, "")).like(term),
                Loan.counterparty_user_id.in_(
                    select(User.id).where(func.lower(User.full_name).like(term))
                ),
            )
        )

    statement: Select = (
        select(Loan)
        .options(selectinload(Loan.payments))
        .where(*conditions)
        # Loans with no due date sort last: a dated obligation is the one that needs
        # attention, so it belongs above one with no deadline at all.
        .order_by(Loan.due_date.is_(None), Loan.due_date, Loan.created_at.desc())
    )

    loans = list(db.scalars(statement))

    if status is not None:
        loans = [loan for loan in loans if _matches_status(loan, status)]

    return loans[offset : offset + limit], len(loans)


def create(db: Session, owner: User, payload: LoanCreate) -> Loan:
    if payload.counterparty_user_id is not None:
        if payload.counterparty_user_id == owner.id:
            raise UnprocessableEntityError(
                "A loan needs two different people.",
                details=[
                    {
                        "field": "counterparty_user_id",
                        "message": "Pick someone else.",
                        "type": "self_loan",
                    }
                ],
            )
        counterparty = db.get(User, payload.counterparty_user_id)
        if counterparty is None or not counterparty.is_active:
            raise UnprocessableEntityError(
                "That person could not be found.",
                details=[
                    {
                        "field": "counterparty_user_id",
                        "message": "Unknown account.",
                        "type": "unknown_user",
                    }
                ],
            )

    loan = Loan(
        owner_id=owner.id,
        direction=payload.direction,
        counterparty_name=payload.counterparty_name,
        counterparty_user_id=payload.counterparty_user_id,
        amount=payload.amount,
        currency=payload.currency,
        due_date=payload.due_date,
        description=payload.description,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


def update(db: Session, loan: Loan, payload: LoanUpdate) -> Loan:
    data = payload.model_dump(exclude_unset=True)

    # `status` is not a column, so it cannot simply be assigned. The two transitions
    # a user can actually ask for are handled explicitly below; the rest are
    # consequences of the payments and are not settable at all.
    wanted_status = data.pop("status", None)

    if "counterparty_user_id" in data and data["counterparty_user_id"] == loan.owner_id:
        raise UnprocessableEntityError(
            "A loan needs two different people.",
            details=[
                {
                    "field": "counterparty_user_id",
                    "message": "Pick someone else.",
                    "type": "self_loan",
                }
            ],
        )

    if "amount" in data and data["amount"] < loan.paid:
        raise UnprocessableEntityError(
            "The loan cannot be less than what has already been repaid.",
            details=[
                {
                    "field": "amount",
                    "message": (
                        f"{loan.paid} has already been repaid against this loan. "
                        "Remove a payment first if the principal was wrong."
                    ),
                    "type": "below_paid",
                }
            ],
        )

    for field, value in data.items():
        setattr(loan, field, value)

    if wanted_status is LoanStatus.CANCELLED:
        cancel(db, loan, commit=False)
    elif wanted_status is not None and loan.cancelled_at is not None:
        # Anything other than CANCELLED reads as "un-cancel this".
        loan.cancelled_at = None

    db.commit()
    db.refresh(loan)
    return loan


def cancel(db: Session, loan: Loan, *, commit: bool = True) -> Loan:
    """Write the loan off. Payments already recorded are kept.

    Cancelling is not deleting: the money did change hands, and a written-off loan
    is a thing people want to be able to look back at.
    """
    if loan.cancelled_at is None:
        loan.cancelled_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
        db.refresh(loan)
    return loan


def delete(db: Session, loan: Loan) -> None:
    db.delete(loan)
    db.commit()


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #


def get_payment_or_404(
    db: Session, payment_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[LoanPayment, Loan]:
    """A payment and its loan, scoped to the owner through the join."""
    row = db.execute(
        select(LoanPayment, Loan)
        .join(Loan, Loan.id == LoanPayment.loan_id)
        .options(selectinload(Loan.payments))
        .where(LoanPayment.id == payment_id, Loan.owner_id == owner_id)
    ).first()

    if row is None:
        raise NotFoundError("Payment not found.")
    return row[0], row[1]


def _check_payable(loan: Loan) -> None:
    if loan.cancelled_at is not None:
        raise ConflictError(
            "This loan was cancelled, so it cannot take a payment. "
            "Reopen it first if that was a mistake."
        )


def add_payment(db: Session, loan: Loan, actor: User, payload: LoanPaymentCreate) -> LoanPayment:
    """Record a repayment.

    **An overpayment flips the balance.** Repay 1,500 against a 1,000 loan and the
    extra 500 is now owed the other way — `signed_balance` goes negative and the
    status becomes `OVERPAID`. Refusing the payment, or swallowing the 500 by
    clamping, would both push the real number outside the app.
    """
    _check_payable(loan)

    payment = LoanPayment(
        loan_id=loan.id,
        amount=payload.amount,
        payment_date=payload.payment_date,
        note=payload.note,
        created_by_id=actor.id,
    )
    db.add(payment)

    # Touched in the same transaction so `updated_at` means "when this loan last
    # changed", which is what the list sorts and the timeline reads.
    loan.updated_at = func.now()

    db.commit()
    db.refresh(payment)
    db.refresh(loan)
    return payment


def settle_in_full(db: Session, loan: Loan, actor: User, *, on: date | None = None) -> LoanPayment:
    """Mark a loan paid by recording the payment that makes it so.

    "Mark as paid" could have been a flag, but a loan whose status says PAID while
    its payments add up to less would be exactly the inconsistency this design
    exists to prevent. Recording the remainder as a real payment keeps the history
    honest and self-explaining: the ledger shows what closed the loan and when.
    """
    _check_payable(loan)

    if loan.remaining <= 0:
        raise ConflictError("This loan is already fully repaid.")

    return add_payment(
        db,
        loan,
        actor,
        LoanPaymentCreate(
            amount=loan.remaining,
            payment_date=on or date.today(),
            note="Settled in full",
        ),
    )


def update_payment(
    db: Session, payment: LoanPayment, loan: Loan, payload: LoanPaymentUpdate
) -> LoanPayment:
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(payment, field, value)

    loan.updated_at = func.now()
    db.commit()
    db.refresh(payment)
    return payment


def delete_payment(db: Session, payment: LoanPayment, loan: Loan) -> None:
    # Removed through the parent's collection rather than with `db.delete()`. The
    # loan is loaded with its payments and the relationship cascades
    # delete-orphan, so a bare delete leaves the collection still holding the row
    # and the flush re-associates it — the payment survives and the loan's
    # `remaining` never moves.
    if payment in loan.payments:
        loan.payments.remove(payment)
    else:
        db.delete(payment)

    loan.updated_at = func.now()
    db.commit()


def payments_for_loan(
    db: Session,
    loan_id: uuid.UUID,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[tuple[LoanPayment, Decimal]], int, Decimal]:
    """Payments newest-first, each with the amount still outstanding after it.

    The remaining-after figure uses a window function over the loan's whole history
    for the same reason the khata ledger does: summed per page it would restart at
    every page boundary, and under a date filter it would restart mid-history.
    """
    running = (
        select(
            LoanPayment.id.label("payment_id"),
            func.sum(LoanPayment.amount)
            .over(
                partition_by=LoanPayment.loan_id,
                order_by=(LoanPayment.payment_date, LoanPayment.created_at, LoanPayment.id),
            )
            .label("paid_so_far"),
        )
        .where(LoanPayment.loan_id == loan_id)
        .subquery()
    )

    conditions = [LoanPayment.loan_id == loan_id]
    if start_date is not None:
        conditions.append(LoanPayment.payment_date >= start_date)
    if end_date is not None:
        conditions.append(LoanPayment.payment_date <= end_date)

    total = db.scalar(select(func.count()).select_from(LoanPayment).where(*conditions)) or 0

    rows = db.execute(
        select(LoanPayment, running.c.paid_so_far)
        .join(running, running.c.payment_id == LoanPayment.id)
        .where(*conditions)
        .order_by(
            LoanPayment.payment_date.desc(),
            LoanPayment.created_at.desc(),
            LoanPayment.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    ).all()

    paid = db.scalar(
        select(func.coalesce(func.sum(LoanPayment.amount), 0)).where(
            LoanPayment.loan_id == loan_id
        )
    )

    return (
        [(row[0], Decimal(row.paid_so_far).quantize(Decimal("0.01"))) for row in rows],
        total,
        Decimal(paid or 0).quantize(Decimal("0.01")),
    )


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #


def balance_with(
    db: Session, owner_id: uuid.UUID, counterparty_user_id: uuid.UUID, currency: str
) -> Decimal:
    """Net loan position with one person, for the person page's unified total.

    Signed the same way as every other balance in the app — positive means they owe
    you — so loans given, loans taken and overpayments all fold into one figure
    without a special case at the call site.

    Cancelled loans are excluded: a written-off loan is not money anyone expects.
    """
    loans = db.scalars(
        select(Loan)
        .options(selectinload(Loan.payments))
        .where(
            Loan.owner_id == owner_id,
            Loan.counterparty_user_id == counterparty_user_id,
            Loan.currency == currency,
            Loan.cancelled_at.is_(None),
        )
    ).all()

    return sum((loan.signed_balance for loan in loans), ZERO).quantize(Decimal("0.01"))


def totals_for_owner(db: Session, owner_id: uuid.UUID) -> list[dict]:
    """Per currency, split by direction.

    The two directions are reported separately rather than netted, because "you are
    owed 50,000 and you owe 30,000" is a different situation from "you are owed
    20,000", and a single net figure cannot tell them apart. `net` is offered
    alongside for anyone who wants the one-number version.
    """
    loans = db.scalars(
        select(Loan).options(selectinload(Loan.payments)).where(Loan.owner_id == owner_id)
    ).all()

    buckets: dict[str, dict] = {}
    for loan in loans:
        bucket = buckets.setdefault(
            loan.currency,
            {
                "currency": loan.currency,
                "lent": ZERO,
                "borrowed": ZERO,
                "repaid_to_you": ZERO,
                "repaid_by_you": ZERO,
                "receivable": ZERO,
                "payable": ZERO,
                # Scoped by direction rather than by sign. `receivable` answers
                # "what am I owed in total"; these answer "where do the loans I
                # gave stand" — which differ once a loan is overpaid, because an
                # overpaid loan you gave is money you owe on a loan you gave.
                "given_balance": ZERO,
                "taken_balance": ZERO,
                "net": ZERO,
                "overdue": ZERO,
                "loan_count": 0,
                "overdue_count": 0,
            },
        )
        bucket["loan_count"] += 1

        if loan.cancelled_at is not None:
            # A written-off loan is money no longer in play, so it appears in
            # neither the lent nor the outstanding figures.
            continue

        given = loan.direction is LoanDirection.GIVEN
        if given:
            bucket["lent"] += loan.amount
            bucket["repaid_to_you"] += loan.paid
            bucket["given_balance"] += loan.signed_balance
        else:
            bucket["borrowed"] += loan.amount
            bucket["repaid_by_you"] += loan.paid
            bucket["taken_balance"] += loan.signed_balance

        # An overpaid loan you gave becomes money you owe, and vice versa — which
        # is exactly what the sign of `signed_balance` already says.
        balance = loan.signed_balance
        if balance > 0:
            bucket["receivable"] += balance
        else:
            bucket["payable"] += -balance
        bucket["net"] += balance

        if loan.status is LoanStatus.OVERDUE:
            bucket["overdue"] += loan.remaining
            bucket["overdue_count"] += 1

    return sorted(buckets.values(), key=lambda bucket: -abs(bucket["net"]))
