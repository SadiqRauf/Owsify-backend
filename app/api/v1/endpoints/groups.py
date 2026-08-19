"""Group CRUD and membership management."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ActiveUser, DbSession
from app.models.group import Group
from app.schemas.common import Message
from app.schemas.expense import ExpenseListPage
from app.schemas.group import (
    GroupCreate,
    GroupDetail,
    GroupMemberAdd,
    GroupMemberRead,
    GroupMemberRoleUpdate,
    GroupRead,
    GroupUpdate,
)
from app.services import expense as expense_service
from app.services import group as group_service

router = APIRouter(prefix="/groups", tags=["groups"])


def _to_read(group: Group, user_id: uuid.UUID) -> GroupRead:
    membership = group.membership_for(user_id)
    return GroupRead(
        id=group.id,
        name=group.name,
        description=group.description,
        currency=group.currency,
        emoji=group.emoji,
        created_by_id=group.created_by_id,
        created_at=group.created_at,
        member_count=len(group.members),
        my_role=membership.role,  # type: ignore[union-attr]
    )


def _to_detail(group: Group, user_id: uuid.UUID) -> GroupDetail:
    return GroupDetail(
        **_to_read(group, user_id).model_dump(),
        members=[GroupMemberRead.model_validate(member) for member in group.members],
    )


@router.get("", response_model=list[GroupRead], summary="Groups you belong to")
def list_groups(db: DbSession, current_user: ActiveUser) -> list[GroupRead]:
    return [
        _to_read(group, current_user.id)
        for group in group_service.list_for_user(db, current_user.id)
    ]


@router.post(
    "", response_model=GroupDetail, status_code=status.HTTP_201_CREATED, summary="Create a group"
)
def create_group(payload: GroupCreate, db: DbSession, current_user: ActiveUser) -> GroupDetail:
    """The creator becomes the owner; `member_ids` are added as ordinary members."""
    group = group_service.create(db, current_user, payload)
    return _to_detail(group, current_user.id)


@router.get("/{group_id}", response_model=GroupDetail, summary="Group detail with members")
def read_group(group_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> GroupDetail:
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    return _to_detail(group, current_user.id)


@router.patch("/{group_id}", response_model=GroupDetail, summary="Update a group")
def update_group(
    group_id: uuid.UUID, payload: GroupUpdate, db: DbSession, current_user: ActiveUser
) -> GroupDetail:
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    group = group_service.update(db, group, current_user.id, payload)
    return _to_detail(group, current_user.id)


@router.delete("/{group_id}", response_model=Message, summary="Delete a group")
def delete_group(group_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> Message:
    """Deletes the group's expenses along with it. Owner only."""
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    group_service.delete(db, group, current_user.id)
    return Message(message="Group deleted.")


@router.get(
    "/{group_id}/members", response_model=list[GroupMemberRead], summary="List group members"
)
def list_members(
    group_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> list[GroupMemberRead]:
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    return [GroupMemberRead.model_validate(member) for member in group.members]


@router.post(
    "/{group_id}/members",
    response_model=GroupDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Add members",
)
def add_members(
    group_id: uuid.UUID, payload: GroupMemberAdd, db: DbSession, current_user: ActiveUser
) -> GroupDetail:
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    group = group_service.add_members(db, group, current_user.id, payload)
    return _to_detail(group, current_user.id)


@router.delete(
    "/{group_id}/members/{user_id}", response_model=Message, summary="Remove a member or leave"
)
def remove_member(
    group_id: uuid.UUID, user_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> Message:
    """Passing your own id leaves the group. Members with expenses cannot be removed.

    Returns a message rather than the group, because leaving means the caller can no
    longer see it.
    """
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    group_service.remove_member(db, group, current_user.id, user_id)

    return Message(
        message="You left the group." if user_id == current_user.id else "Member removed."
    )


@router.patch(
    "/{group_id}/members/{user_id}", response_model=GroupDetail, summary="Change a member's role"
)
def set_member_role(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: GroupMemberRoleUpdate,
    db: DbSession,
    current_user: ActiveUser,
) -> GroupDetail:
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    group = group_service.set_role(db, group, current_user.id, user_id, payload.role)
    return _to_detail(group, current_user.id)


@router.post(
    "/{group_id}/transfer-ownership/{user_id}",
    response_model=GroupDetail,
    summary="Hand ownership to another member",
)
def transfer_ownership(
    group_id: uuid.UUID, user_id: uuid.UUID, db: DbSession, current_user: ActiveUser
) -> GroupDetail:
    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)
    group = group_service.transfer_ownership(db, group, current_user.id, user_id)
    return _to_detail(group, current_user.id)


@router.get(
    "/{group_id}/expenses", response_model=ExpenseListPage, summary="This group's expense history"
)
def list_group_expenses(
    group_id: uuid.UUID,
    db: DbSession,
    current_user: ActiveUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExpenseListPage:
    from app.api.v1.endpoints.expenses import to_expense_read

    group = group_service.get_or_404(db, group_id)
    group_service.require_membership(group, current_user.id)

    items, total = expense_service.list_for_user(
        db, current_user.id, group_id=group_id, limit=limit, offset=offset
    )
    return ExpenseListPage(
        items=[to_expense_read(expense, current_user.id) for expense in items],
        total=total,
        limit=limit,
        offset=offset,
    )
