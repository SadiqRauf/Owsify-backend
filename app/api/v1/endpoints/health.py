"""Liveness and readiness probes."""

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import DbSession
from app.core.config import settings
from app.schemas.common import HealthStatus

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthStatus, summary="Liveness + database check")
def health(db: DbSession, response: Response) -> HealthStatus:
    """Report process health and whether the database answers a trivial query."""
    database = "connected"
    details: dict[str, object] = {}

    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        database = "unavailable"
        details["database_error"] = exc.__class__.__name__
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthStatus(
        status="ok" if database == "connected" else "degraded",
        version=settings.VERSION,
        environment=settings.ENVIRONMENT,
        database=database,
        details=details,
    )


@router.get("/health/live", summary="Liveness only", tags=["health"])
def liveness() -> dict[str, str]:
    """Cheap check that never touches the database."""
    return {"status": "ok"}
