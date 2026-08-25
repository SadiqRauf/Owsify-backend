"""Authentication request/response schemas."""

from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserRead


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds.")


class AuthResponse(TokenPair):
    """What register and login return: tokens plus the user they belong to."""

    user: UserRead


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="Session to revoke. Omit to revoke every session for the user.",
    )


class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(examples=["ahmed@example.com"])


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(
        min_length=8,
        max_length=72,
        description="Same rules as registration. 72 bytes is bcrypt's own limit.",
    )


class ResetTokenCheck(BaseModel):
    """What a reset page needs before it asks for a new password."""

    valid: bool
    email: EmailStr | None = Field(
        default=None,
        description=(
            "Only returned for a token that is already valid, so this cannot be used "
            "to look up an address without one."
        ),
    )
