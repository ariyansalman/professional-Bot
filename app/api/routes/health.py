"""Liveness and readiness endpoints (section 123).

Neither endpoint exposes hostnames, DSNs, versions of dependencies or any
credential. ``/health`` proves the process is alive; ``/ready`` proves its
dependencies are reachable and is what a load balancer should gate on.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.schemas.common import HealthResponse
from app.core.logging import get_logger
from app.core.redis import redis_health
from app.db.session import get_sessionmaker

log = get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    from app import __version__

    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=HealthResponse, summary="Readiness probe")
async def ready(response: Response) -> HealthResponse:
    from app import __version__

    checks: dict[str, str] = {}
    healthy = True

    try:
        factory = get_sessionmaker()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        log.error("health.database_unavailable", error=str(exc)[:300])
        checks["database"] = "unavailable"
        healthy = False

    redis_ok, _ = await redis_health()
    checks["redis"] = "ok" if redis_ok else "unavailable"
    if not redis_ok:
        # Redis loss degrades locking and rate limiting but does not corrupt
        # financial state, so the service reports degraded rather than dead.
        checks["note"] = "degraded"

    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status="ok" if healthy and redis_ok else ("degraded" if healthy else "unavailable"),
        version=__version__,
        checks=checks,
    )
