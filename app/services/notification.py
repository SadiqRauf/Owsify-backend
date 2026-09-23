"""Writing, reading and rendering notifications.

The rule every caller relies on: `notify` only *adds* to the session. It never
commits, so the notification lands in the same transaction as the change it
describes, and a change that fails takes its notification with it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.expense import Expense
from app.models.group import Group, GroupRole
from app.models.notification import Notification, NotificationType
from app.models.settlement import Settlement

T = NotificationType


def notify(
    db: Session,
    recipient_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None,
    type: NotificationType,
    data: dict[str, Any] | None = None,
) -> None:
    """Queue one notification. Nobody is ever notified about their own action."""
    if recipient_id == actor_id:
        return
    db.add(
        Notification(
            recipient_id=recipient_id,
            actor_id=actor_id,
            type=type,
            data=data or {},
            # Stamped here, not by the column's `now()`: Postgres fixes that at the
            # start of the transaction, so everything one request wrote would tie
            # and the newest-first order would come back arbitrary.
            created_at=datetime.now(UTC),
        )
    )


# --------------------------------------------------------------------------- #
# Snapshots
#
# Everything is stored as strings: JSONB has no decimal type, and a float would
# turn 30.10 into 30.099999999999998 on the way back out.
# --------------------------------------------------------------------------- #
def _group_data(group: Group | None) -> dict[str, Any]:
    if group is None:
        return {}
    return {"group_id": str(group.id), "group_name": group.name}


def expense_data(expense: Expense, recipient_id: uuid.UUID) -> dict[str, Any]:
    return {
        "expense_id": str(expense.id),
        "description": expense.description,
        "amount": str(expense.amount),
        "currency": expense.currency,
        "your_share": str(expense.share_for(recipient_id)),
        **_group_data(expense.group),
    }


def settlement_data(settlement: Settlement) -> dict[str, Any]:
    return {
        "settlement_id": str(settlement.id),
        "amount": str(settlement.amount),
        "currency": settlement.currency,
        "from_user_id": str(settlement.from_user_id),
        "from_user_name": settlement.from_user.full_name,
        "to_user_id": str(settlement.to_user_id),
        "to_user_name": settlement.to_user.full_name,
        **_group_data(settlement.group),
    }


def group_data(group: Group, **extra: Any) -> dict[str, Any]:
    return _group_data(group) | extra


# --------------------------------------------------------------------------- #
# Queries and commands
# --------------------------------------------------------------------------- #
def list_for_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    conditions = [Notification.recipient_id == user_id]
    if unread_only:
        conditions.append(Notification.read_at.is_(None))

    total = db.scalar(select(func.count()).select_from(Notification).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return items, total


def unread_count(db: Session, user_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.recipient_id == user_id, Notification.read_at.is_(None))
        )
        or 0
    )


def get_or_404(db: Session, notification_id: uuid.UUID, recipient_id: uuid.UUID) -> Notification:
    notification = db.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.recipient_id == recipient_id
        )
    )
    if notification is None:
        raise NotFoundError("Notification not found.")
    return notification


def mark_read(db: Session, notification: Notification) -> Notification:
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(notification)
    return notification


def mark_all_read(db: Session, user_id: uuid.UUID) -> int:
    result = db.execute(
        update(Notification)
        .where(Notification.recipient_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    db.commit()
    return result.rowcount or 0


# --------------------------------------------------------------------------- #
# Rendering
#
# Done at read time from the snapshot, so the wording can be improved without a
# data migration, and the actor's current name is used when they still exist.
# --------------------------------------------------------------------------- #
def _money(data: dict[str, Any], key: str = "amount") -> str:
    return f"{data.get('currency', '')} {Decimal(data.get(key, '0')):,.2f}".strip()


def _person(data: dict[str, Any], side: str, viewer_id: uuid.UUID) -> str:
    if data.get(f"{side}_user_id") == str(viewer_id):
        return "you"
    return data.get(f"{side}_user_name") or "someone"


def _in_group(data: dict[str, Any]) -> str:
    return f" in {data['group_name']}" if data.get("group_name") else ""


def message_for(notification: Notification) -> str:
    data = notification.data
    actor = notification.actor.full_name if notification.actor else "Someone"
    viewer = notification.recipient_id
    group = data.get("group_name", "a group")
    share = Decimal(data.get("your_share", "0"))

    match notification.type:
        case T.EXPENSE_ADDED:
            text = f'{actor} added "{data.get("description")}" ({_money(data)}){_in_group(data)}.'
            return text + (f" Your share is {_money(data, 'your_share')}." if share else "")
        case T.EXPENSE_UPDATED:
            text = f'{actor} edited "{data.get("description")}"{_in_group(data)}.'
            if share:
                return text + f" Your share is now {_money(data, 'your_share')}."
            return text + " You are no longer part of it."
        case T.EXPENSE_DELETED:
            return f'{actor} deleted "{data.get("description")}" ({_money(data)}){_in_group(data)}.'
        case T.SETTLEMENT_RECORDED:
            payer, payee = _person(data, "from", viewer), _person(data, "to", viewer)
            if data.get("from_user_id") == str(notification.actor_id):
                return f"{actor} paid {payee} {_money(data)}{_in_group(data)}."
            return f"{actor} recorded that {payer} paid {payee} {_money(data)}{_in_group(data)}."
        case T.SETTLEMENT_UPDATED:
            payer, payee = _person(data, "from", viewer), _person(data, "to", viewer)
            return f"{actor} changed the payment from {payer} to {payee} to {_money(data)}."
        case T.SETTLEMENT_DELETED:
            payer, payee = _person(data, "from", viewer), _person(data, "to", viewer)
            return f"{actor} deleted the payment of {_money(data)} from {payer} to {payee}."
        case T.FRIEND_REQUEST:
            return f"{actor} sent you a friend request."
        case T.FRIEND_ACCEPTED:
            return f"{actor} accepted your friend request."
        case T.GROUP_ADDED:
            return f"{actor} added you to {group}."
        case T.GROUP_REMOVED:
            return f"{actor} removed you from {group}."
        case T.GROUP_ROLE_CHANGED:
            role = "an admin" if data.get("role") == GroupRole.ADMIN.value else "a member"
            return f"{actor} made you {role} of {group}."
        case T.GROUP_OWNERSHIP_TRANSFERRED:
            return f"{actor} made you the owner of {group}."
        case T.GROUP_DELETED:
            return f"{actor} deleted the group {group}."
        case T.INVITATION_ACCEPTED:
            return f"{actor} joined from your invitation. You are now friends."
    return f"{actor} did something that involves you."


def href_for(notification: Notification) -> str | None:
    """Where tapping the notification should take you, in the web app's routes."""
    data = notification.data
    group_id = data.get("group_id")

    match notification.type:
        case T.EXPENSE_ADDED | T.EXPENSE_UPDATED:
            return f"/expenses/{data['expense_id']}"
        case T.EXPENSE_DELETED:
            return f"/groups/{group_id}" if group_id else "/activity"
        case T.SETTLEMENT_RECORDED | T.SETTLEMENT_UPDATED | T.SETTLEMENT_DELETED:
            return f"/groups/{group_id}" if group_id else "/settlements"
        case T.FRIEND_REQUEST | T.FRIEND_ACCEPTED | T.INVITATION_ACCEPTED:
            return "/friends"
        case T.GROUP_ADDED | T.GROUP_ROLE_CHANGED | T.GROUP_OWNERSHIP_TRANSFERRED:
            return f"/groups/{group_id}"
        case T.GROUP_REMOVED | T.GROUP_DELETED:
            return "/groups"
    return None
