"""Password reset tokens.

**The token is stored hashed, never in plain text.** A reset token is a temporary
password: anyone holding one can take the account. Storing them readable would mean
a leaked database backup hands over every account with a live reset in flight, which
is exactly the failure that hashing passwords exists to prevent. A plain SHA-256 is
the right tool here rather than bcrypt — the token is 32 bytes of `secrets` output,
so there is no low-entropy guess space for a slow hash to defend, and reset
verification happens on a request path where bcrypt's cost would be felt.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        # Reset flows look a token up by its hash, and clear a user's outstanding
        # tokens when a new one is issued. Those are the only two access patterns.
        Index("ix_password_reset_tokens_user_used", "user_id", "used_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # SHA-256 of the token we emailed. 64 hex characters.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Single use. A timestamp rather than a boolean, so "when was this used" stays
    # answerable — which matters when working out what an attacker did and when.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Recorded for the same reason: the account holder should be able to see where a
    # reset came from, and it costs nothing to keep.
    requested_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    user: Mapped["User"] = relationship()  # noqa: F821

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.expires_at > datetime.now(UTC)

    def __repr__(self) -> str:
        return f"<PasswordResetToken for {self.user_id} used={self.used_at is not None}>"
