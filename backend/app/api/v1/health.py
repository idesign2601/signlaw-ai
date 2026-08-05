"""Liveness and readiness endpoints.

These live outside the versioned API prefix because orchestrators and load
balancers should not have to track API versions.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.deps import SettingsDep, get_engine
from app.db.session import ping
from app.schemas.health import (
    ComponentHealth,
    HealthResponse,
    HealthStatus,
    ReadinessResponse,
)

__all__ = ["router"]

router = APIRouter(tags=["health"])


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Reports that the process is running. Does not touch any dependency, so "
        "a failing database never causes the container to be restarted."
    ),
)
async def liveness(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status=HealthStatus.OK,
        service=settings.app_name,
        version=settings.version,
        environment=settings.environment.value,
    )


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    description=(
        "Checks every dependency needed to serve traffic. Returns 503 when any "
        "required component is unavailable."
    ),
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(
    settings: SettingsDep,
    response: Response,
    engine: Annotated[AsyncEngine, Depends(get_engine)],
) -> ReadinessResponse:
    components = [await _check_database(engine)]

    overall = (
        HealthStatus.OK
        if all(component.status is HealthStatus.OK for component in components)
        else HealthStatus.UNAVAILABLE
    )
    if overall is not HealthStatus.OK:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status=overall,
        service=settings.app_name,
        version=settings.version,
        environment=settings.environment.value,
        components=components,
    )


async def _check_database(engine: AsyncEngine) -> ComponentHealth:
    started = time.perf_counter()
    healthy = await ping(engine)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)

    return ComponentHealth(
        name="database",
        status=HealthStatus.OK if healthy else HealthStatus.UNAVAILABLE,
        detail=None if healthy else "Postgres did not respond to a connectivity probe.",
        latency_ms=latency_ms,
    )
