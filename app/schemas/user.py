"""User request/response schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.currencies import DEFAULT_CURRENCY, normalise_currency
from app.core.security import MAX_PASSWORD_BYTES

PASSWORD_RULES = (
    f"8-{MAX_PASSWORD_BYTES} characters, with at least one letter and one number."
)


def _validate_password_strength(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes long.")
    if not any(char.isalpha() for char in value):
        raise ValueError("Password must contain at least one letter.")
    if not any(char.isdigit() for char in value):
        raise ValueError("Password must contain at least one number.")
    return value


class UserBase(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    currency: str = Field(default=DEFAULT_CURRENCY, min_length=3, max_length=3)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str) -> str:
        return normalise_currency(value)


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=128, description=PASSWORD_RULES)

    _check_password = field_validator("password")(_validate_password_strength)


class UserUpdate(BaseModel):
    """Every field optional — this backs a PATCH."""

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    avatar_url: str | None = Field(default=None, max_length=512)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Name cannot be blank.")
        return stripped

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, value: str | None) -> str | None:
        return normalise_currency(value) if value else value


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8, max_length=128, description=PASSWORD_RULES)

    _check_password = field_validator("new_password")(_validate_password_strength)


class UserRead(UserBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    avatar_url: str | None = None
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None
