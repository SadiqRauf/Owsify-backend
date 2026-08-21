"""Loans: a fixed principal, the payments against it, and where it stands."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.core.exceptions import BadRequestError
from app.models.loan import Loan, LoanDirection, LoanStatus
from app.schemas.common import Message
from app.schemas.loan import (
    LoanCreate,
    LoanCurrencyTotal,
    LoanDetail,
    LoanListPage,
    LoanPaymentCreate,
    LoanPaymentListPage,
    LoanPaymentRead,
    LoanPaymentUpdate,
    LoanPaymentWithProgress,
    LoanRead,
    LoanUpdate,
)
from app.schemas.user import UserRead
from app.services import loan as loan_service

# Two routers, as with khata entries: payments are created and listed under their
# loan, but addressed directly once they exist. The direct router is registered
# first in router.py so `/loans/payments/{id}` is not read as a loan id.
payment_router = APIRouter(prefix="/loans/payments", tags=["loans"])
router = APIRouter(prefix="/loans", tags=["loans"])


def _days_until_due(loan: Loan) -> int | None:
    """Null for a loan with no date, and for one that is closed.

    A countdown on a settled loan is noise: there is nothing left to be late for.
    """
    if loan.due_date is None or not loan.is_outstanding:
        return None
    return (loan.due_date - date.today()).days


def _to_read(loan: Loan) -> LoanRead:
    return LoanRead(
        id=loan.id,
        direction=loan.direction,
        counterparty_name=loan.counterparty_name,
        display_name=loan.display_name,
        counterparty_user=(
            UserRead.model_validate(loan.counterparty_user)
            if loan.counterparty_user
            else None
        ),
        amount=loan.amount,
        currency=loan.currency,
        due_date=loan.due_date,
        description=loan.description,
        created_at=loan.created_at,
        updated_at=loan.updated_at,
        cancelled_at=loan.cancelled_at,
        paid=loan.paid,
        remaining=loan.remaining,
        overpaid=loan.overpaid,
        signed_balance=loan.signed_balance,
        status=loan.status,
        payment_count=len(loan.payments),
        days_until_due=_days_until_due(loan),
    )


def _to_detail(loan: Loan) -> LoanDetail:
    return LoanDetail(
        **_to_read(loan).model_dump(),
        payments=[LoanPaymentRead.model_validate(payment) for payment in loan.payments],
    )


@router.get("", response_model=LoanListPage, summary="Loans you have given and taken")
def list_loans(
    db: DbSession,
    current_user: ActiveUser,
    status_filter: Annotated[
        LoanStatus | None, Query(alias="status", description="Only loans in this state.")
    ] = None,
    direction: Annotated[
        LoanDirection | None, Query(description="Only loans you gave, or only ones you took.")
    ] = None,
    counterparty_user_id: Annotated[
        uuid.UUID | None, Query(description="Only this person.")
    ] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    search: Annotated[str | None, Query(max_length=200, description="Name or description.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LoanListPage:
    """Soonest-due first, with loans that have no due date last.

    `paid`, `remaining` and `status` are computed from each loan's payments, so this
    list can never disagree with a loan's own detail page. `totals` describes all
    your loans and ignores the filters, so a filtered view cannot make an
    outstanding book look clear.
    """
    loans, total = loan_service.list_for_owner(
        db,
        current_user.id,
        status=status_filter,
        direction=direction,
        counterparty_user_id=counterparty_user_id,
        currency=currency,
        search=search,
        limit=limit,
        offset=offset,
    )

    return LoanListPage(
        items=[_to_read(loan) for loan in loans],
        total=total,
        limit=limit,
        offset=offset,
        totals=[
            LoanCurrencyTotal(**bucket)
            for bucket in loan_service.totals_for_owner(db, current_user.id)
        ],
    )


@router.post("", response_model=LoanDetail, status_code=status.HTTP_201_CREATED, summary="Record a loan")
def create_loan(payload: LoanCreate, db: DbSession, current_user: ActiveUser) -> LoanDetail:
    """`direction: given` if you lent it, `taken` if you borrowed it.

    A name is enough — the other person does not need an account.

    Link one with `counterparty_user_id` when they have it, and the loan will fold
    into their unified person total.
    """
    loan = loan_service.create(db, current_user, payload)
    return _to_detail(loan)


@router.get("/{loan_id}", response_model=LoanDetail, summary="One loan, with its payments")
def read_loan(loan_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> LoanDetail:
    return _to_detail(loan_service.get_or_404(db, loan_id, current_user.id))


@router.patch("/{loan_id}", response_model=LoanDetail, summary="Edit a loan")
def update_loan(
    loan_id: uuid.UUID, payload: LoanUpdate, db: DbSession, current_user: ActiveUser
) -> LoanDetail:
    """The principal, the direction, the other person, the due date and cancellation.

    The principal cannot be lowered below what has already been repaid — that would
    describe a loan its own history contradicts. To mark a loan paid, use
    `POST /loans/{id}/settle`, which records the payment that closes it rather than
    setting a flag the payments do not support.
    """
    loan = loan_service.get_or_404(db, loan_id, current_user.id)
    return _to_detail(loan_service.update(db, loan, payload))


@router.delete("/{loan_id}", response_model=Message, summary="Cancel or delete a loan")
def delete_loan(
    loan_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    permanent: Annotated[
        bool, Query(description="Destroy the loan and its payments instead of writing it off.")
    ] = False,
) -> Message:
    """Cancels by default, keeping the payment history.

    As with a khata, the forgiving action belongs on the unqualified verb: the money
    did change hands, and a written-off loan is something a lender wants to look
    back at.
    """
    loan = loan_service.get_or_404(db, loan_id, current_user.id)

    if permanent:
        loan_service.delete(db, loan)
        return Message(message="Loan deleted.")

    loan_service.cancel(db, loan)
    return Message(message="Loan cancelled.")


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #


@router.get(
    "/{loan_id}/payments", response_model=LoanPaymentListPage, summary="A loan's repayments"
)
def list_payments(
    loan_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    start_date: Annotated[date | None, Query(description="Inclusive lower bound.")] = None,
    end_date: Annotated[date | None, Query(description="Inclusive upper bound.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LoanPaymentListPage:
    """Newest first, each payment carrying what was still outstanding after it.

    That figure is computed over the loan's whole history, so it keeps its meaning
    on page two and under a date filter.
    """
    if start_date and end_date and start_date > end_date:
        raise BadRequestError("The start date must not be after the end date.")

    loan = loan_service.get_or_404(db, loan_id, current_user.id)
    rows, total, paid = loan_service.payments_for_loan(
        db, loan.id, start_date=start_date, end_date=end_date, limit=limit, offset=offset
    )

    return LoanPaymentListPage(
        items=[
            LoanPaymentWithProgress(
                **LoanPaymentRead.model_validate(payment).model_dump(),
                remaining_after=max(loan.amount - paid_so_far, 0),
            )
            for payment, paid_so_far in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
        currency=loan.currency,
        direction=loan.direction,
        loan_amount=loan.amount,
        paid=paid,
        remaining=loan.remaining,
        overpaid=loan.overpaid,
    )


@router.post(
    "/{loan_id}/payments",
    response_model=LoanDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Record a repayment",
)
def add_payment(
    loan_id: uuid.UUID, payload: LoanPaymentCreate, db: DbSession, current_user: ActiveUser
) -> LoanDetail:
    """Partial payments are the normal case, and are what `remaining` is for.

    **An overpayment flips the balance rather than being clamped away.** Repay 1,500
    against a 1,000 loan and `overpaid` is 500, `signed_balance` goes negative and the
    status becomes `overpaid` — the extra 500 is now owed the other way. The whole
    loan is returned, because a payment always moves the figures around it.
    """
    loan = loan_service.get_or_404(db, loan_id, current_user.id)
    loan_service.add_payment(db, loan, current_user, payload)
    return _to_detail(loan_service.get_or_404(db, loan_id, current_user.id))


@router.post("/{loan_id}/settle", response_model=LoanDetail, summary="Mark a loan fully paid")
def settle_loan(loan_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> LoanDetail:
    """Records a payment for whatever is left, rather than setting a flag.

    A loan marked PAID whose payments add up to less than the principal is exactly
    the inconsistency this design avoids, so "mark as paid" writes the payment that
    makes it true. The history then shows what closed the loan and when.
    """
    loan = loan_service.get_or_404(db, loan_id, current_user.id)
    loan_service.settle_in_full(db, loan, current_user)
    return _to_detail(loan_service.get_or_404(db, loan_id, current_user.id))


@payment_router.patch("/{payment_id}", response_model=LoanDetail, summary="Edit a repayment")
def update_payment(
    payment_id: uuid.UUID, payload: LoanPaymentUpdate, db: DbSession, current_user: ActiveUser
) -> LoanDetail:
    payment, loan = loan_service.get_payment_or_404(db, payment_id, current_user.id)
    loan_service.update_payment(db, payment, loan, payload)
    return _to_detail(loan_service.get_or_404(db, loan.id, current_user.id))


@payment_router.delete("/{payment_id}", response_model=Message, summary="Delete a repayment")
def delete_payment(payment_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    """Removing a payment puts the amount back on the loan, because `remaining` is
    only ever the principal less these rows."""
    payment, loan = loan_service.get_payment_or_404(db, payment_id, current_user.id)
    loan_service.delete_payment(db, payment, loan)
    return Message(message="Payment deleted.")
