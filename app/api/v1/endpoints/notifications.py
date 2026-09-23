"""Your notifications: what other people did that affects you."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ActiveUser, DbSession
from app.models.notification import Notification
from app.schemas.common import Message
from app.schemas.notification import NotificationListPage, NotificationRead, UnreadCount
from app.schemas.user import UserRead
from app.services import notification as notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _read(notification: Notification) -> NotificationRead:
    return NotificationRead(
        id=notification.id,
        type=notification.type,
        actor=UserRead.model_validate(notification.actor) if notification.actor else None,
        message=notification_service.message_for(notification),
        href=notification_service.href_for(notification),
        data=notification.data,
        is_read=notification.is_read,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


@router.get("", response_model=NotificationListPage, summary="Your notifications")
def list_notifications(
    db: DbSession,
    current_user: ActiveUser,
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> NotificationListPage:
    """Newest first."""
    items, total = notification_service.list_for_user(
        db, current_user.id, unread_only=unread_only, limit=limit, offset=offset
    )
    return NotificationListPage(
        items=[_read(item) for item in items],
        total=total,
        limit=limit,
        offset=offset,
        unread_count=notification_service.unread_count(db, current_user.id),
    )


@router.get("/unread-count", response_model=UnreadCount, summary="How many are unread")
def unread_count(db: DbSession, current_user: ActiveUser) -> UnreadCount:
    """Cheap enough to poll: it is what the bell badge reads."""
    return UnreadCount(count=notification_service.unread_count(db, current_user.id))


@router.post("/read-all", response_model=Message, summary="Mark everything read")
def mark_all_read(db: DbSession, current_user: ActiveUser) -> Message:
    marked = notification_service.mark_all_read(db, current_user.id)
    return Message(message=f"Marked {marked} notification{'' if marked == 1 else 's'} as read.")


@router.post("/{notification_id}/read", response_model=NotificationRead, summary="Mark one read")
def mark_read(notification_id: uuid.UUID, db: DbSession, current_user: ActiveUser) -> NotificationRead:
    notification = notification_service.get_or_404(db, notification_id, current_user.id)
    return _read(notification_service.mark_read(db, notification))
