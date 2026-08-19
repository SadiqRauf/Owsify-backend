"""Invitation schemas."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.invitation import InvitationStatus
from app.schemas.user import UserRead


class InvitationCreate(BaseModel):
    email: EmailStr
    message: str | None = Field(
        default=None,
        max_length=500,
        description="Optional note included in the email.",
    )


class InvitationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    status: InvitationStatus
    message: str | None = None
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None = None
    invited_by: UserRead

    is_expired: bool = Field(description="True once expires_at has passed.")

    # How this message was handled, so a client never claims an email was sent
    # when the server is only logging it. Sending happens in a background task,
    # so the configured mode is what is knowable at response time — not the
    # eventual success of an individual SMTP conversation.
    delivery: Literal["email", "console", "file"] = Field(
        default="console",
        description=(
            "email = really sent over SMTP; console = written to the server log; "
            "file = written to EMAIL_FILE_PATH."
        ),
    )
