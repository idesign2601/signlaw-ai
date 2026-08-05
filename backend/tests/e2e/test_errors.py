"""Error contract.

Every failure, whatever raised it, must come back as RFC 7807
``application/problem+json`` carrying the correlation ID. Clients and the
frontend depend on that single shape.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.core.exceptions import (
    IndexNotReadyError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)

PROBLEM_JSON = "application/problem+json"


class _Payload(BaseModel):
    city: str
    max_results: int


@pytest.fixture
def error_app(app: FastAPI) -> FastAPI:
    """Mount routes that raise each error class the API can produce."""

    @app.get("/_test/not-found")
    async def _not_found() -> None:
        raise NotFoundError("Document", "abc-123")

    @app.get("/_test/domain-validation")
    async def _domain_validation() -> None:
        raise ValidationError("City 'Atlantis' is not a BC municipality.")

    @app.get("/_test/index-not-ready")
    async def _index_not_ready() -> None:
        raise IndexNotReadyError()

    @app.get("/_test/rate-limited")
    async def _rate_limited() -> None:
        raise RateLimitError(retry_after_s=30)

    @app.get("/_test/unexpected")
    async def _unexpected() -> None:
        raise RuntimeError("something broke internally")

    @app.post("/_test/echo")
    async def _echo(payload: _Payload) -> dict[str, str | int]:
        return {"city": payload.city, "max_results": payload.max_results}

    return app


@pytest.fixture
async def error_client(error_app: FastAPI):
    from asgi_lifespan import LifespanManager

    async with LifespanManager(error_app):
        transport = ASGITransport(app=error_app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client


class TestDomainErrors:
    async def test_not_found_shape(self, error_client: AsyncClient) -> None:
        response = await error_client.get("/_test/not-found")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_JSON)

        body = response.json()
        assert body["code"] == "not_found"
        assert body["status"] == 404
        assert body["title"] == "Not Found"
        assert body["instance"] == "/_test/not-found"
        assert body["type"].endswith("/not_found")
        assert body["details"]["resource"] == "Document"
        assert body["details"]["identifier"] == "abc-123"

    async def test_domain_validation_is_422(self, error_client: AsyncClient) -> None:
        response = await error_client.get("/_test/domain-validation")
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"

    async def test_index_not_ready_is_503(self, error_client: AsyncClient) -> None:
        response = await error_client.get("/_test/index-not-ready")
        assert response.status_code == 503
        assert response.json()["code"] == "index_not_ready"
        # The message must tell a first-time operator what to do.
        assert "ingestion" in response.json()["detail"].lower()

    async def test_rate_limit_sets_retry_after_header(self, error_client: AsyncClient) -> None:
        response = await error_client.get("/_test/rate-limited")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "30"

    async def test_every_error_carries_the_request_id(self, error_client: AsyncClient) -> None:
        response = await error_client.get(
            "/_test/not-found", headers={"X-Request-ID": "trace-me"}
        )
        assert response.json()["request_id"] == "trace-me"
        assert response.headers["X-Request-ID"] == "trace-me"


class TestUnexpectedErrors:
    async def test_internals_are_not_leaked(self, error_client: AsyncClient) -> None:
        with pytest.raises(RuntimeError):
            # ASGITransport re-raises server exceptions by default; the handler
            # response is asserted below with raise_app_exceptions disabled.
            await error_client.get("/_test/unexpected")

    async def test_returns_a_generic_500_problem(self, error_app: FastAPI) -> None:
        from asgi_lifespan import LifespanManager

        transport = ASGITransport(app=error_app, raise_app_exceptions=False)
        async with LifespanManager(error_app):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/_test/unexpected")

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == "internal_error"
        # Debug is off, so the underlying message must not reach the client.
        assert "something broke internally" not in body["detail"]

    async def test_500_still_carries_a_correlation_id(self, error_app: FastAPI) -> None:
        # The context variable is already torn down by the time Starlette's
        # outermost error middleware runs, so this exercises the request.state
        # fallback — a 500 is exactly when the ID is most needed.
        from asgi_lifespan import LifespanManager

        transport = ASGITransport(app=error_app, raise_app_exceptions=False)
        async with LifespanManager(error_app):
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/_test/unexpected", headers={"X-Request-ID": "trace-500"}
                )

        assert response.json()["request_id"] == "trace-500"
        assert response.headers["X-Request-ID"] == "trace-500"


class TestRequestValidation:
    async def test_missing_field_is_reported_per_field(self, error_client: AsyncClient) -> None:
        response = await error_client.post("/_test/echo", json={"city": "Burnaby"})

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "validation_error"
        assert any(error["field"].endswith("max_results") for error in body["errors"])

    async def test_wrong_type_is_reported(self, error_client: AsyncClient) -> None:
        response = await error_client.post(
            "/_test/echo", json={"city": "Burnaby", "max_results": "many"}
        )
        assert response.status_code == 422
        assert response.json()["errors"]

    async def test_valid_payload_passes(self, error_client: AsyncClient) -> None:
        response = await error_client.post(
            "/_test/echo", json={"city": "Burnaby", "max_results": 5}
        )
        assert response.status_code == 200
        assert response.json() == {"city": "Burnaby", "max_results": 5}


class TestHttpExceptions:
    async def test_unknown_route_uses_the_problem_shape(self, client: AsyncClient) -> None:
        response = await client.get("/does-not-exist")

        assert response.status_code == 404
        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert response.json()["code"] == "http_404"

    async def test_method_not_allowed(self, client: AsyncClient) -> None:
        response = await client.post("/healthz")
        assert response.status_code == 405
        assert response.json()["code"] == "http_405"
