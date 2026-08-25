"""Application settings, loaded from environment variables and the .env file."""

import logging
from email.utils import formataddr, parseaddr
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import PostgresDsn, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


# Recognisable so the production guard below can reject it by identity rather
# than trying to guess whether a key "looks" real.
DEV_SECRET_KEY = "dev-only-insecure-secret-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Application ---
    PROJECT_NAME: str = "Owsify API"
    VERSION: str = "0.1.0"
    DESCRIPTION: str = "Expense sharing and settlement API."
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # --- Server ---
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # --- Database ---
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = ""
    POSTGRES_DB: str = "splitwise"
    DATABASE_URL: str | None = None
    SQL_ECHO: bool = False
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800

    # --- Security / JWT ---
    SECRET_KEY: str = DEV_SECRET_KEY
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    BCRYPT_ROUNDS: int = 12

    # --- Email ---
    # "console" logs the message, "file" writes .eml files, "smtp" really sends.
    EMAIL_BACKEND: Literal["console", "file", "smtp"] = "console"
    EMAIL_FROM: str = "Owsify <no-reply@owsify.local>"
    EMAIL_FILE_PATH: str = "./sent-emails"
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    SMTP_TIMEOUT_SECONDS: int = 10

    # --- Invitations ---
    # Where the invite link points, i.e. the web app rather than the API.
    FRONTEND_URL: str = "http://localhost:5173"
    INVITATION_EXPIRE_DAYS: int = 14

    # A reset link is a temporary password, so it is short-lived by design. Long
    # enough to survive a slow mail server and a distracted user; short enough that
    # a link left in an inbox is not a standing key to the account.
    PASSWORD_RESET_EXPIRE_MINUTES: int = 60

    # Sending mail on an unauthenticated endpoint is a spam vector aimed at other
    # people's inboxes, so requests per address are capped over a window.
    PASSWORD_RESET_MAX_PER_WINDOW: int = 3
    PASSWORD_RESET_WINDOW_MINUTES: int = 15

    # --- CORS ---
    # NoDecode stops pydantic-settings from JSON-parsing the raw value, so the
    # validator below can accept a plain comma-separated list in .env.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    CORS_ALLOW_CREDENTIALS: bool = True

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str) and not value.startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_uri(self) -> str:
        """Full SQLAlchemy URL. An explicit DATABASE_URL always wins."""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return str(
            PostgresDsn.build(
                scheme="postgresql+psycopg2",
                username=self.POSTGRES_USER,
                password=self.POSTGRES_PASSWORD or None,
                host=self.POSTGRES_HOST,
                port=self.POSTGRES_PORT,
                path=self.POSTGRES_DB,
            )
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @model_validator(mode="after")
    def _normalise_gmail_smtp(self) -> "Settings":
        """Make a Gmail account work the way people actually configure it.

        Two things go wrong with Gmail specifically, and both are silent:

        **App passwords are displayed with spaces.** Google shows a 16-character
        app password as `abcd efgh ijkl mnop`, and pasting it verbatim fails with a
        bare "Username and Password not accepted" that says nothing about spaces.
        They are stripped for Gmail hosts only — an arbitrary provider could
        legitimately have a space in a password, but Google's never do.

        **Gmail refuses to send as anyone but the authenticated account.** Leaving
        `EMAIL_FROM` as the default would mean either a rejection or, worse, Gmail
        quietly rewriting the header so mail arrives from an address the app never
        chose. Aligning it here — keeping the display name, replacing the address —
        makes what is going to happen anyway explicit, and says so in the log.
        """
        if not self._is_gmail_smtp:
            return self

        if self.SMTP_PASSWORD:
            stripped = "".join(self.SMTP_PASSWORD.split())
            if stripped != self.SMTP_PASSWORD:
                object.__setattr__(self, "SMTP_PASSWORD", stripped)

        if self.SMTP_USER:
            name, address = parseaddr(self.EMAIL_FROM)
            if address.lower() != self.SMTP_USER.lower():
                object.__setattr__(
                    self,
                    "EMAIL_FROM",
                    formataddr((name or "Owsify", self.SMTP_USER)),
                )
                logging.getLogger(__name__).warning(
                    "Gmail only sends as the authenticated account, so EMAIL_FROM "
                    "was changed from %r to %r. Set EMAIL_FROM to your Gmail "
                    "address (or a verified alias) to silence this.",
                    address or self.EMAIL_FROM,
                    self.SMTP_USER,
                )

        return self

    @property
    def _is_gmail_smtp(self) -> bool:
        return self.EMAIL_BACKEND == "smtp" and self.SMTP_HOST.lower().endswith(
            ("smtp.gmail.com", "smtp.googlemail.com")
        )

    @model_validator(mode="after")
    def _refuse_unsafe_production_config(self) -> "Settings":
        """Fail fast rather than serve production traffic with dev defaults.

        A placeholder signing key in production means anyone who has read the
        source can mint a valid token for any account. That has to be a startup
        crash, not a warning someone scrolls past.
        """
        if self.ENVIRONMENT != "production":
            return self

        problems: list[str] = []

        if self.SECRET_KEY == DEV_SECRET_KEY or len(self.SECRET_KEY) < 32:
            problems.append(
                "SECRET_KEY must be set to a unique value of at least 32 characters "
                '(generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))")'
            )

        if self.DEBUG:
            problems.append("DEBUG must be false in production; it leaks internals in error responses")

        if any(origin == "*" for origin in self.CORS_ORIGINS):
            problems.append("CORS_ORIGINS must name real origins, never '*'")

        if problems:
            raise ValueError(
                "Refusing to start in production with an unsafe configuration:\n  - "
                + "\n  - ".join(problems)
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so the .env file is only parsed once per process."""
    return Settings()


settings = get_settings()
