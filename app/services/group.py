"""Group creation, membership, and role management."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)
from app.models.expense import Expense, ExpenseSplit
from app.models.group import Group, GroupMember, GroupRole
from app.models.user import User
from app.schemas.group import GroupCreate, GroupMemberAdd, GroupUpdate
from app.services import user as user_service


def get_or_404(db: Session, group_id: uuid.UUID) -> Group:
    group = db.get(Group, group_id)
    if group is None:
        raise NotFoundError("Group not found.")
    return group


def require_membership(group: Group, user_id: uuid.UUID) -> GroupMember:
    membership = group.membership_for(user_id)
    if membership is None:
        # Deliberately a 404: revealing that a group exists to a non-member leaks
        # its existence to anyone who can guess an id.
        raise NotFoundError("Group not found.")
    return membership


def require_manager(group: Group, user_id: uuid.UUID) -> GroupMember:
    membership = require_membership(group, user_id)
    if not membership.role.can_manage_members:
        raise PermissionDeniedError("Only the group owner or an admin can do that.")
    return membership


def list_for_user(db: Session, user_id: uuid.UUID) -> list[Group]:
    return list(
        db.scalars(
            select(Group)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == user_id)
            .order_by(Group.created_at.desc())
        )
    )


def create(db: Session, creator: User, payload: GroupCreate) -> Group:
    group = Group(
        name=payload.name,
        description=payload.description,
        currency=payload.currency,
        emoji=payload.emoji,
        created_by_id=creator.id,
    )
    group.members.append(GroupMember(user_id=creator.id, role=GroupRole.OWNER))

    for user_id in dict.fromkeys(payload.member_ids):
        if user_id == creator.id:
            continue
        member = user_service.get_by_id(db, user_id)
        if member is None or not member.is_active:
            raise NotFoundError(f"User {user_id} does not exist.")
        group.members.append(GroupMember(user_id=user_id, role=GroupRole.MEMBER))

    db.add(group)
    db.commit()
    db.refresh(group)
    return group


def update(db: Session, group: Group, user_id: uuid.UUID, payload: GroupUpdate) -> Group:
    require_manager(group, user_id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(group, field, value)

    db.commit()
    db.refresh(group)
    return group


def delete(db: Session, group: Group, user_id: uuid.UUID) -> None:
    membership = require_membership(group, user_id)
    if membership.role is not GroupRole.OWNER:
        raise PermissionDeniedError("Only the group owner can delete a group.")

    db.delete(group)
    db.commit()


def add_members(db: Session, group: Group, actor_id: uuid.UUID, payload: GroupMemberAdd) -> Group:
    require_manager(group, actor_id)

    targets: list[User] = []
    for user_id in payload.user_ids:
        user = user_service.get_by_id(db, user_id)
        if user is None or not user.is_active:
            raise NotFoundError(f"User {user_id} does not exist.")
        targets.append(user)

    for email in payload.emails:
        user = user_service.get_by_email(db, str(email))
        if user is None or not user.is_active:
            raise NotFoundError(f"No account exists for {email}.")
        targets.append(user)

    existing_ids = {member.user_id for member in group.members}
    added = 0
    for user in targets:
        if user.id in existing_ids:
            continue
        group.members.append(GroupMember(user_id=user.id, role=payload.role))
        existing_ids.add(user.id)
        added += 1

    if added == 0:
        raise ConflictError("Everyone you listed is already in this group.")

    db.commit()
    db.refresh(group)
    return group


def remove_member(db: Session, group: Group, actor_id: uuid.UUID, target_id: uuid.UUID) -> Group:
    """Remove someone, or leave the group when removing yourself."""
    actor = require_membership(group, actor_id)

    if target_id != actor_id and not actor.role.can_manage_members:
        raise PermissionDeniedError("Only the group owner or an admin can remove members.")

    target = group.membership_for(target_id)
    if target is None:
        raise NotFoundError("That person is not in this group.")

    if target.role is GroupRole.OWNER:
        raise BadRequestError(
            "The owner cannot leave. Transfer ownership first, or delete the group."
        )

    if _has_unsettled_activity(db, group.id, target_id):
        raise ConflictError(
            "That member still appears in this group's expenses. "
            "Delete or reassign those expenses first."
        )

    group.members.remove(target)
    db.commit()
    db.refresh(group)
    return group


def set_role(db: Session, group: Group, actor_id: uuid.UUID, target_id: uuid.UUID, role: GroupRole) -> Group:
    actor = require_membership(group, actor_id)
    if actor.role is not GroupRole.OWNER:
        raise PermissionDeniedError("Only the group owner can change roles.")

    target = group.membership_for(target_id)
    if target is None:
        raise NotFoundError("That person is not in this group.")
    if target.role is GroupRole.OWNER:
        raise BadRequestError("Transfer ownership instead of changing the owner's role.")

    target.role = role
    db.commit()
    db.refresh(group)
    return group


def transfer_ownership(db: Session, group: Group, actor_id: uuid.UUID, target_id: uuid.UUID) -> Group:
    actor = require_membership(group, actor_id)
    if actor.role is not GroupRole.OWNER:
        raise PermissionDeniedError("Only the current owner can transfer ownership.")

    target = group.membership_for(target_id)
    if target is None:
        raise NotFoundError("That person is not in this group.")
    if target.id == actor.id:
        raise BadRequestError("You already own this group.")

    actor.role = GroupRole.ADMIN
    target.role = GroupRole.OWNER
    db.commit()
    db.refresh(group)
    return group


def _has_unsettled_activity(db: Session, group_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """True if the user paid for, or owes on, any expense in this group."""
    paid = db.scalar(
        select(func.count())
        .select_from(Expense)
        .where(Expense.group_id == group_id, Expense.paid_by_id == user_id)
    )
    owes = db.scalar(
        select(func.count())
        .select_from(ExpenseSplit)
        .join(Expense, Expense.id == ExpenseSplit.expense_id)
        .where(Expense.group_id == group_id, ExpenseSplit.user_id == user_id)
    )
    return bool(paid) or bool(owes)


def member_count(db: Session, group_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)
        )
        or 0
    )
