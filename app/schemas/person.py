"""One person, seen across groups, settlements and khatas."""

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserRead


class PersonBalanceBreakdown(BaseModel):
    """Where the total comes from.

    Every figure is signed the same way: **positive means they owe you**, negative
    means you owe them. One convention across three subsystems, so the numbers can
    simply be added.
    """

    group_balance: Decimal = Field(
        description="Net across shared groups, already net of settlements between you."
    )
    khata_balance: Decimal = Field(description="Net across the khatas you keep for them.")
    loan_balance: Decimal = Field(
        default=Decimal("0.00"),
        description=(
            "Always 0.00 — there is no loans feature in the app yet. The field is "
            "reported rather than omitted so the total is visibly complete, and it "
            "starts carrying value the day loans exist."
        ),
    )
    total_balance: Decimal = Field(description="group + khata + loan.")
    settled_total: Decimal = Field(
        description=(
            "Gross settled between you, for context only. Already applied to "
            "group_balance, so it is not part of the total."
        )
    )


class PersonSummaryRead(BaseModel):
    person: UserRead
    currency: str
    balances: PersonBalanceBreakdown
    shared_group_count: int
    khata_count: int
    expense_count: int
    khata_ids: list[uuid.UUID]
    shared_groups: list["PersonGroupRef"]
    recent_activity: list["PersonActivityRead"]


class PersonGroupRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class PersonActivityRead(BaseModel):
    id: uuid.UUID
    kind: str = Field(description="expense, settlement or khata_entry.")
    occurred_at: datetime
    summary: str
    amount: Decimal
    currency: str
    your_impact: Decimal = Field(
        description="Positive when this left you owed money, negative when owing."
    )
    group_id: uuid.UUID | None = None
    group_name: str | None = None
    khata_id: uuid.UUID | None = None


class PersonActivityPage(BaseModel):
    items: list[PersonActivityRead]
    total: int
    limit: int
    offset: int


class PersonListItem(BaseModel):
    """A row on the people list.

    `user` is null for a khata-only contact: someone with no account cannot have a
    `/people/{id}` summary, so the row points at their khata instead. Hiding them
    would be worse — from the owner's side they are exactly as real as anyone else.
    """

    id: uuid.UUID
    name: str
    email: str | None = None
    avatar_url: str | None = None
    user: UserRead | None = None
    khata_id: uuid.UUID | None = None
    currency: str
    total_balance: Decimal
    has_account: bool


class PersonListPage(BaseModel):
    items: list[PersonListItem]
    total: int
