"""Application error types and the handlers that turn them into JSON responses.

Every error the API returns uses one envelope, so the frontend only has to know
about a single shape:

    {"error": {"code": "...", "message": "...", "details": [...]}, "request_id": "..."}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

# Starlette deprecated HTTP_422_UNPROCESSABLE_ENTITY in favour of a name that is
# not available in every supported version, so pin the value directly.
HTTP_422_UNPROCESSABLE_CONTENT = 422


class AppError(Exception):
    """Base class for every error this application raises deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "internal_error"
    message: str = "Something went wrong."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or []
        self.headers = headers
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_code = "bad_request"
    message = "The request could not be processed."


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_code = "unauthenticated"
    message = "Authentication is required."

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kwargs)


class InvalidCredentialsError(AuthenticationError):
    error_code = "invalid_credentials"
    message = "Incorrect email or password."


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_code = "permission_denied"
    message = "You do not have access to this resource."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "not_found"
    message = "The requested resource does not exist."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_code = "conflict"
    message = "The resource conflicts with existing data."


class EmailAlreadyRegisteredError(ConflictError):
    error_code = "email_already_registered"
    message = "An account with this email already exists."


class UnprocessableEntityError(AppError):
    status_code = HTTP_422_UNPROCESSABLE_CONTENT
    error_code = "validation_error"
    message = "The submitted data is invalid."


# --------------------------------------------------------------------------- #
# Response helper
# --------------------------------------------------------------------------- #
def error_response(
    request: Request,
    *,
    status_code: int,
    error_code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": error_code,
                "message": message,
                "details": details or [],
            },
            "request_id": getattr(request.state, "request_id", None),
        },
    )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(
        request,
        status_code=exc.status_code,
        error_code=exc.error_code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Reuse FastAPI's own status phrases as machine codes, e.g. 404 -> "not_found".
    code = {
        400: "bad_request",
        401: "unauthenticated",
        403: "permission_denied",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        429: "rate_limited",
    }.get(exc.status_code, "http_error")
    return error_response(
        request,
        status_code=exc.status_code,
        error_code=code,
        message=str(exc.detail),
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    details = [
        {
            # Drop the leading "body"/"query" segment for a field name the UI can use.
            "field": ".".join(str(part) for part in error["loc"][1:]) or str(error["loc"][0]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return error_response(
        request,
        status_code=HTTP_422_UNPROCESSABLE_CONTENT,
        error_code="validation_error",
        message="The submitted data is invalid.",
        details=details,
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    logger.warning("Database integrity error: %s", exc, exc_info=settings.DEBUG)
    return error_response(
        request,
        status_code=status.HTTP_409_CONFLICT,
        error_code="conflict",
        message="The operation conflicts with existing data.",
    )


async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Unhandled database error")
    return error_response(
        request,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        error_code="database_unavailable",
        message="The database is currently unavailable.",
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled application error")
    return error_response(
        request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code="internal_error",
        # Never leak internals in production; in development the traceback is in the log.
        message=str(exc) if settings.DEBUG else "Something went wrong.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_exception_handler)
