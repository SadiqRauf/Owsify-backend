"""Recording payments between people, and their history."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.schemas.common import Message
from app.schemas.settlement import (
    SettlementCreate,
    SettlementListPage,
    SettlementRead,
    SettlementUpdate,
)
from app.services import settlement as settlement_service

router = APIRouter(prefix="/settlements", tags=["settlements"])


@router.get("", response_model=SettlementListPage, summary="Settlement history")
def list_settlements(
    db: DbSession,
    current_user: ActiveUser,
    group_id: Annotated[uuid.UUID | None, Query(description="Only this group.")] = None,
    with_user_id: Annotated[uuid.UUID | None, Query(description="Only with this person.")] = None,
    sort: Annotated[
        Literal["-settled_on", "settled_on", "-amount", "amount", "-created_at", "created_at"],
        Query(description="Prefix with - for descending."),
    ] = "-settled_on",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SettlementListPage:
    items, total = settlement_service.list_for_user(
        db,
        current_user.id,
        group_id=group_id,
        with_user_id=with_user_id,
        limit=limit,
        offset=offset,
        sort=sort,
    )
    return SettlementListPage(
        items=[SettlementRead.model_validate(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=SettlementRead,
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment",
)
def create_settlement(
    payload: SettlementCreate, db: DbSession, current_user: ActiveUser
) -> SettlementRead:
    """`from_user_id` paid `to_user_id`, which reduces what they owe them.

    In a group the settlement takes the group's currency, both people must be
    members, and any member may record it. Outside a group you must be one of the
    two people and the other must be a friend.
    """
    settlement = settlement_service.create(db, current_user, payload)
    return SettlementRead.model_validate(settlement)


@router.get("/{settlement_id}", response_model=SettlementRead, summary="One settlement")
def read_settlement(
    settlement_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> SettlementRead:
    settlement = settlement_service.get_or_404(db, settlement_id, current_user.id)
    return SettlementRead.model_validate(settlement)


@router.patch("/{settlement_id}", response_model=SettlementRead, summary="Edit a settlement")
def update_settlement(
    settlement_id: uuid.UUID,
    payload: SettlementUpdate,
    db: DbSession,
    current_user: ActiveUser,
) -> SettlementRead:
    settlement = settlement_service.get_or_404(db, settlement_id, current_user.id)
    settlement = settlement_service.update(db, settlement, current_user, payload)
    return SettlementRead.model_validate(settlement)


@router.delete("/{settlement_id}", response_model=Message, summary="Delete a settlement")
def delete_settlement(
    settlement_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> Message:
    """Removing a settlement restores the debt it discharged."""
    settlement = settlement_service.get_or_404(db, settlement_id, current_user.id)
    settlement_service.delete(db, settlement, current_user)
    return Message(message="Settlement deleted.")
