"""Report payloads. Every figure is scoped to one currency and one window."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class ReportWindow(BaseModel):
    """Echoed back so a saved or shared report is self-describing."""

    start_date: date
    end_date: date
    currency: str
    label: str = Field(description="A human title for the window, e.g. 'August 2026'.")


class MoneyFlowRead(BaseModel):
    money_given: Decimal = Field(description="Khata given, plus adjustments that increased debt.")
    money_received: Decimal = Field(description="Khata received, plus adjustments that reduced it.")
    loans_given: Decimal = Field(description="Principal you lent out in the window.")
    loans_taken: Decimal = Field(description="Principal you borrowed in the window.")
    loan_payments: Decimal = Field(description="Repayments that came back to you.")
    loan_repayments_made: Decimal = Field(description="Repayments you made on loans you took.")
    expenses_paid: Decimal = Field(description="Your share of group expenses in the window.")
    settlements_in: Decimal
    settlements_out: Decimal

    khata_receivable: Decimal = Field(
        description=(
            "Outstanding across all khatas as of now, deliberately not windowed: "
            "what you are owed is a position, not a flow, and it does not reset "
            "because a month ended."
        )
    )
    loans_receivable: Decimal = Field(
        description="Owed to you across all open loans, as of now. Also a position."
    )
    loans_payable: Decimal = Field(
        description=(
            "Owed by you across all open loans, including any that were overpaid and "
            "so now run the other way. Reported beside `loans_receivable` rather than "
            "netted, because being owed 50,000 while owing 30,000 is not the same "
            "situation as being owed 20,000."
        )
    )

    net_flow: Decimal = Field(
        description="Received + repayments + settlements in, less given + lent + settlements out."
    )


class SummaryReport(BaseModel):
    window: ReportWindow
    flow: MoneyFlowRead
    available_currencies: list[str] = Field(
        description=(
            "Currencies you have money actually recorded in, most active first, and "
            "empty when you have none. Lets a client open Reports on a currency with "
            "something in it rather than on a page of zeroes. Your own currency is "
            "not padded in, so an empty list genuinely means no activity anywhere."
        )
    )


class KhataReportRow(BaseModel):
    person_user_id: uuid.UUID | None
    name: str
    khata_id: uuid.UUID | None
    given: Decimal
    received: Decimal
    balance: Decimal = Field(description="As it stands now, not only within the window.")
    entry_count: int


class KhataReportTotals(BaseModel):
    given: Decimal
    received: Decimal
    balance: Decimal


class KhataReport(BaseModel):
    window: ReportWindow
    rows: list[KhataReportRow]
    totals: KhataReportTotals


class LoanReportRowRead(BaseModel):
    loan_id: uuid.UUID
    counterparty_name: str
    direction: str
    person_user_id: uuid.UUID | None
    amount: Decimal
    paid: Decimal
    remaining: Decimal
    overpaid: Decimal
    signed_balance: Decimal = Field(description="Positive means they owe you.")
    status: str
    due_date: date | None
    paid_in_window: Decimal
    given_in_window: bool


class LoanReportTotals(BaseModel):
    lent: Decimal
    borrowed: Decimal
    repaid: Decimal
    receivable: Decimal
    payable: Decimal
    net: Decimal
    repaid_in_window: Decimal
    lent_in_window: Decimal
    borrowed_in_window: Decimal
    overdue: Decimal


class LoanReport(BaseModel):
    window: ReportWindow
    rows: list[LoanReportRowRead]
    totals: LoanReportTotals


class ActivityPoint(BaseModel):
    period: str = Field(description="YYYY-MM for monthly, YYYY-MM-DD for daily.")
    given: Decimal
    received: Decimal
    lent: Decimal
    repaid: Decimal


class ActivityReport(BaseModel):
    window: ReportWindow
    granularity: str
    points: list[ActivityPoint] = Field(
        description="Empty periods are returned as zero, so a chart gets an even axis."
    )
