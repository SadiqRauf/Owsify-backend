"""Reports: what moved over a window, and what is still outstanding."""

import calendar
import uuid
from dataclasses import asdict
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import ActiveUser, DbSession
from app.core.currencies import normalise_currency
from app.core.exceptions import BadRequestError
from app.schemas.report import (
    ActivityPoint,
    ActivityReport,
    KhataReport,
    KhataReportRow,
    KhataReportTotals,
    LoanReport,
    LoanReportRowRead,
    LoanReportTotals,
    MoneyFlowRead,
    ReportWindow,
    SummaryReport,
)
from app.services import report as report_service

router = APIRouter(prefix="/reports", tags=["reports"])

# A year of daily buckets is already a very wide chart; beyond that the request is
# almost certainly a mistake rather than an intention.
MAX_WINDOW_DAYS = 1826  # five years


def _resolve_window(
    start_date: date | None, end_date: date | None, currency: str
) -> ReportWindow:
    """Default to the current month, which is what a report is usually asked for.

    The window is echoed back on every response so a report that is saved, printed
    or shared still says what it covers.
    """
    today = date.today()

    if end_date is None:
        end_date = (
            date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
            if start_date is None
            else today
        )
    if start_date is None:
        start_date = date(end_date.year, end_date.month, 1)

    if start_date > end_date:
        raise BadRequestError("The start date must not be after the end date.")
    if (end_date - start_date).days > MAX_WINDOW_DAYS:
        raise BadRequestError("That window is too wide. Ask for five years or less.")

    # A window that is exactly one calendar month gets that month's name, which is
    # the title in the brief. Anything else spells out both ends.
    if (
        start_date.day == 1
        and start_date.year == end_date.year
        and start_date.month == end_date.month
        and end_date.day == calendar.monthrange(end_date.year, end_date.month)[1]
    ):
        label = start_date.strftime("%B %Y")
    else:
        label = f"{start_date.isoformat()} to {end_date.isoformat()}"

    return ReportWindow(
        start_date=start_date, end_date=end_date, currency=currency, label=label
    )


@router.get("/summary", response_model=SummaryReport, summary="What moved this period")
def read_summary(
    db: DbSession,
    current_user: ActiveUser,
    start_date: Annotated[date | None, Query(description="Defaults to the 1st of this month.")] = None,
    end_date: Annotated[date | None, Query(description="Defaults to the end of this month.")] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    person_user_id: Annotated[uuid.UUID | None, Query(description="Narrow to one person.")] = None,
) -> SummaryReport:
    """The headline figures: given, received, lent, repaid, and what is outstanding.

    Flows are windowed; positions (`khata_receivable`, `loans_outstanding`) are not,
    because what you are owed does not reset because a month ended.
    """
    code = normalise_currency(currency) if currency else current_user.currency
    window = _resolve_window(start_date, end_date, code)

    flow = report_service.money_flow(
        db,
        current_user,
        start_date=window.start_date,
        end_date=window.end_date,
        currency=code,
        person_user_id=person_user_id,
    )

    return SummaryReport(
        window=window,
        available_currencies=report_service.available_currencies(db, current_user),
        flow=MoneyFlowRead(
            money_given=flow.money_given,
            money_received=flow.money_received,
            loans_given=flow.loans_given,
            loans_taken=flow.loans_taken,
            loan_payments=flow.loan_payments,
            loan_repayments_made=flow.loan_repayments_made,
            expenses_paid=flow.expenses_paid,
            settlements_in=flow.settlements_in,
            settlements_out=flow.settlements_out,
            khata_receivable=flow.khata_receivable,
            loans_receivable=flow.loans_receivable,
            loans_payable=flow.loans_payable,
            net_flow=flow.net_flow,
        ),
    )


@router.get("/khata", response_model=KhataReport, summary="Khata activity per person")
def read_khata_report(
    db: DbSession,
    current_user: ActiveUser,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    person_user_id: Annotated[uuid.UUID | None, Query()] = None,
) -> KhataReport:
    """Given and received in the window, beside the balance as it stands.

    Two different questions, side by side on purpose: a quiet month must not be able
    to read as a settled book.
    """
    code = normalise_currency(currency) if currency else current_user.currency
    window = _resolve_window(start_date, end_date, code)

    rows, totals = report_service.khata_report(
        db,
        current_user,
        start_date=window.start_date,
        end_date=window.end_date,
        currency=code,
        person_user_id=person_user_id,
    )

    return KhataReport(
        window=window,
        rows=[KhataReportRow(**asdict(row)) for row in rows],
        totals=KhataReportTotals(**totals),
    )


@router.get("/loans", response_model=LoanReport, summary="Loans and repayments")
def read_loan_report(
    db: DbSession,
    current_user: ActiveUser,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    person_user_id: Annotated[uuid.UUID | None, Query()] = None,
    include_cancelled: Annotated[bool, Query(description="Include written-off loans.")] = False,
) -> LoanReport:
    """Every loan with where it stands, and what was repaid inside the window.

    Loans given and taken are both included, distinguished by `direction`, and the
    totals report the two sides separately rather than as one net figure.
    """
    code = normalise_currency(currency) if currency else current_user.currency
    window = _resolve_window(start_date, end_date, code)

    rows, totals = report_service.loan_report(
        db,
        current_user,
        start_date=window.start_date,
        end_date=window.end_date,
        currency=code,
        person_user_id=person_user_id,
        include_cancelled=include_cancelled,
    )

    return LoanReport(
        window=window,
        rows=[LoanReportRowRead(**asdict(row)) for row in rows],
        totals=LoanReportTotals(**totals),
    )


@router.get("/activity", response_model=ActivityReport, summary="Money in and out over time")
def read_activity(
    db: DbSession,
    current_user: ActiveUser,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    granularity: Annotated[Literal["daily", "monthly"], Query()] = "monthly",
) -> ActivityReport:
    """Given, received, lent and repaid per period, for the activity chart.

    Empty periods come back as zero rather than being omitted, so the chart gets an
    even time axis instead of silently compressing the quiet stretches.
    """
    code = normalise_currency(currency) if currency else current_user.currency
    window = _resolve_window(start_date, end_date, code)

    points = report_service.activity_series(
        db,
        current_user,
        start_date=window.start_date,
        end_date=window.end_date,
        currency=code,
        granularity=granularity,
    )

    return ActivityReport(
        window=window,
        granularity=granularity,
        points=[ActivityPoint(**asdict(point)) for point in points],
    )
