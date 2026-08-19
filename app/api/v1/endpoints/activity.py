"""A merged feed of expenses and settlements."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.api.deps import ActiveUser, DbSession
from app.schemas.settlement import ActivityGroupRef, ActivityItemRead, ActivityPage
from app.schemas.user import UserRead
from app.services import activity as activity_service
from app.services import group as group_service
from app.services.activity import ActivityType

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("", response_model=ActivityPage, summary="Everything that has happened")
def read_activity(
    db: DbSession,
    current_user: ActiveUser,
    group_id: Annotated[uuid.UUID | None, Query(description="Only this group.")] = None,
    with_user_id: Annotated[uuid.UUID | None, Query(description="Only involving this person.")] = None,
    type: Annotated[
        Literal["expense", "settlement"] | None,
        Query(description="Restrict to one kind of entry."),
    ] = None,
    order: Annotated[Literal["desc", "asc"], Query(description="Newest or oldest first.")] = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ActivityPage:
    """Expenses and settlements, newest first, scoped to what you can see.

    Derived from the underlying tables rather than a separate log, so it can never
    describe something that no longer exists.
    """
    if group_id is not None:
        group = group_service.get_or_404(db, group_id)
        group_service.require_membership(group, current_user.id)

    items, total = activity_service.feed(
        db,
        current_user.id,
        group_id=group_id,
        with_user_id=with_user_id,
        types={ActivityType(type)} if type else None,
        limit=limit,
        offset=offset,
        order=order,
    )

    return ActivityPage(
        items=[
            ActivityItemRead(
                id=item.id,
                type=item.type.value,
                occurred_at=item.occurred_at,
                actor=UserRead.model_validate(item.actor),
                summary=item.summary,
                amount=item.amount,
                currency=item.currency,
                group=ActivityGroupRef.model_validate(item.group) if item.group else None,
                counterparty=(
                    UserRead.model_validate(item.counterparty) if item.counterparty else None
                ),
                your_impact=item.your_impact,
            )
            for item in items
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
