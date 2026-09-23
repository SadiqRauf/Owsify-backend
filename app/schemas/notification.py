"""Notification payloads."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.notification import NotificationType
from app.schemas.user import UserRead


class NotificationRead(BaseModel):
    id: uuid.UUID
    type: NotificationType
    actor: UserRead | None = Field(description="Who caused it. Null if their account is gone.")
    message: str = Field(examples=['Alice added "Dinner" (PKR 60.00) in Trip. Your share is PKR 30.00.'])
    href: str | None = Field(description="Where this leads in the web app.")
    data: dict[str, Any] = Field(description="The snapshot the message was rendered from.")
    is_read: bool
    read_at: datetime | None
    created_at: datetime


class NotificationListPage(BaseModel):
    items: list[NotificationRead]
    total: int
    limit: int
    offset: int
    unread_count: int = Field(description="Across all notifications, unaffected by the filter.")


class UnreadCount(BaseModel):
    count: int
