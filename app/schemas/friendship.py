"""Friend request and friend list schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.friendship import FriendshipStatus
from app.schemas.user import UserRead


class FriendRequestCreate(BaseModel):
    """Address a request by user id or by email — whichever the UI has to hand."""

    user_id: uuid.UUID | None = None
    email: EmailStr | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "FriendRequestCreate":
        if bool(self.user_id) == bool(self.email):
            raise ValueError("Provide either user_id or email, not both.")
        return self


class FriendshipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: FriendshipStatus
    created_at: datetime
    responded_at: datetime | None = None

    # Resolved against the caller, so the client never has to work out which side
    # of the row it is looking at.
    user: UserRead = Field(description="The other person in this friendship.")
    is_incoming: bool = Field(description="True when the other person sent the request.")


class FriendSummary(BaseModel):
    """A confirmed friend, flattened for list rendering."""

    model_config = ConfigDict(from_attributes=True)

    friendship_id: uuid.UUID
    user: UserRead
    friends_since: datetime | None = None


class UserSearchResult(UserRead):
    """A search hit, annotated with how the caller already relates to them."""

    relationship: str = Field(
        description="none, self, friends, request_sent, or request_received.",
        examples=["none"],
    )
