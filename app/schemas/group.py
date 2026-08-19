"""Group and membership schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.core.currencies import DEFAULT_CURRENCY, normalise_currency
from app.models.group import GroupRole
from app.schemas.user import UserRead


class GroupMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user: UserRead
    role: GroupRole
    joined_at: datetime = Field(validation_alias="created_at")


class GroupBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)
    emoji: str | None = Field(default=None, max_length=8)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Group name cannot be blank.")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        return normalise_currency(value)


class GroupCreate(GroupBase):
    member_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=50,
        description="Users to add alongside the creator, who is always the owner.",
    )


class GroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    emoji: str | None = Field(default=None, max_length=8)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Group name cannot be blank.")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str | None) -> str | None:
        return normalise_currency(value) if value else value


class GroupRead(GroupBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_by_id: uuid.UUID
    created_at: datetime
    member_count: int
    # The caller's own role, so the UI knows which controls to render.
    my_role: GroupRole


class GroupDetail(GroupRead):
    members: list[GroupMemberRead]


class GroupMemberAdd(BaseModel):
    user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    emails: list[EmailStr] = Field(default_factory=list, max_length=50)
    role: GroupRole = GroupRole.MEMBER

    @model_validator(mode="after")
    def _at_least_one(self) -> "GroupMemberAdd":
        if not self.user_ids and not self.emails:
            raise ValueError("Provide at least one user_id or email.")
        if self.role is GroupRole.OWNER:
            raise ValueError("A group has exactly one owner; transfer ownership instead.")
        return self


class GroupMemberRoleUpdate(BaseModel):
    role: GroupRole

    @model_validator(mode="after")
    def _not_owner(self) -> "GroupMemberRoleUpdate":
        if self.role is GroupRole.OWNER:
            raise ValueError("Use the ownership transfer endpoint to change owner.")
        return self
