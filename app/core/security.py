"""Password hashing and JWT issuing / verification.

Access tokens are short-lived and stateless. Refresh tokens carry a ``jti`` that is
mirrored by a row in ``refresh_tokens``, which is what makes revocation possible.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import bcrypt
import jwt

from app.core.config import settings

# bcrypt truncates anything past 72 bytes, so we reject longer input outright
# rather than silently accepting a weaker password than the user typed.
MAX_PASSWORD_BYTES = 72


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class TokenPayload:
    subject: str
    token_type: TokenType
    jti: str
    expires_at: datetime
    issued_at: datetime


class TokenError(Exception):
    """Raised when a token is malformed, expired, or of an unexpected type."""


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes.")
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(encoded, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    encoded = plain_password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, hashed_password.encode("utf-8"))
    except ValueError:
        # Stored hash is not a valid bcrypt digest.
        return False


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    jti: str | None = None,
) -> tuple[str, str, datetime]:
    """Return ``(encoded_token, jti, expires_at)``."""
    now = datetime.now(UTC)
    expires_at = now + expires_delta
    token_id = jti or uuid.uuid4().hex

    claims = {
        "sub": subject,
        "type": token_type.value,
        "jti": token_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    encoded = jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded, token_id, expires_at


def create_access_token(subject: str | uuid.UUID) -> tuple[str, datetime]:
    token, _, expires_at = _create_token(
        str(subject),
        TokenType.ACCESS,
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return token, expires_at


def create_refresh_token(subject: str | uuid.UUID) -> tuple[str, str, datetime]:
    """Return ``(token, jti, expires_at)`` — persist the jti to allow revocation."""
    return _create_token(
        str(subject),
        TokenType.REFRESH,
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_token(token: str, expected_type: TokenType) -> TokenPayload:
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid.") from exc

    actual_type = claims.get("type")
    if actual_type != expected_type.value:
        raise TokenError(f"Expected a {expected_type.value} token, got {actual_type!r}.")

    return TokenPayload(
        subject=claims["sub"],
        token_type=TokenType(actual_type),
        jti=claims["jti"],
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        issued_at=datetime.fromtimestamp(claims["iat"], tz=UTC),
    )
