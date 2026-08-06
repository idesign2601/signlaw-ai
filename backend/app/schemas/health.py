"""Health and readiness response contracts."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ComponentHealth", "HealthResponse", "HealthStatus", "ReadinessResponse"]


class HealthStatus(StrEnum):
    """Overall or per-component health."""

    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ComponentHealth(BaseModel):
    """Health of a single dependency."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Component identifier, e.g. 'database'.")
    status: HealthStatus
    # Populated only when the component is not OK, to keep the payload small.
    detail: str | None = Field(default=None, description="Why the component is not healthy.")
    latency_ms: float | None = Field(default=None, description="How long the probe took.")


class HealthResponse(BaseModel):
    """Liveness: the process is up and serving. Never touches dependencies."""

    model_config = ConfigDict(frozen=True)

    status: HealthStatus = HealthStatus.OK
    service: str
    version: str
    environment: str


class ReadinessResponse(BaseModel):
    """Readiness: every dependency required to serve traffic is reachable.

    Returned with HTTP 503 when ``status`` is not ``ok``, so orchestrators pull
    the instance out of rotation rather than sending it doomed requests.
    """

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    service: str
    version: str
    environment: str
    components: list[ComponentHealth]
