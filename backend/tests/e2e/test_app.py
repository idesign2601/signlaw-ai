"""Application wiring: metadata, docs exposure, CORS and compression."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from app.core.config import Environment, LogFormat
from app.main import create_app


class TestOpenApi:
    async def test_schema_is_served_outside_production(self, client: AsyncClient) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        assert response.json()["info"]["title"]

    async def test_docs_are_served_outside_production(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 200

    async def test_health_routes_are_documented(self, client: AsyncClient) -> None:
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/healthz" in paths
        assert "/readyz" in paths

    async def test_description_carries_the_legal_disclaimer(self, client: AsyncClient) -> None:
        # Users must never mistake this for legal advice, including via the API.
        description = (await client.get("/openapi.json")).json()["info"]["description"]
        assert "not legal advice" in description.lower()


class TestProductionApp:
    def _production_app(self, settings_factory):
        return create_app(
            settings_factory(
                environment=Environment.PRODUCTION,
                debug=False,
                db={"password": "a-real-production-password"},
                security={
                    "admin_api_key": "k" * 64,
                    # Production refuses to boot without client keys too: an
                    # unauthenticated /ask is an open GPU inference endpoint.
                    "api_keys": ["c" * 64],
                    "cors_origins": ["https://app.example.com"],
                },
                observability={"log_format": LogFormat.JSON},
            )
        )

    async def test_docs_are_disabled(self, settings_factory) -> None:
        app = self._production_app(settings_factory)
        transport = ASGITransport(app=app)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # The schema describes admin routes; it is not published by default.
            assert (await client.get("/openapi.json")).status_code == 404
            assert (await client.get("/docs")).status_code == 404

    def test_configuration_is_attached_to_state(self, settings_factory) -> None:
        app = self._production_app(settings_factory)
        assert app.state.settings.environment is Environment.PRODUCTION


class TestCors:
    async def test_allowed_origin_is_reflected(self, client: AsyncClient) -> None:
        response = await client.get(
            "/healthz", headers={"Origin": "http://localhost:5173"}
        )
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"

    async def test_disallowed_origin_is_not_reflected(self, client: AsyncClient) -> None:
        response = await client.get("/healthz", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    async def test_preflight_is_answered(self, client: AsyncClient) -> None:
        response = await client.options(
            "/healthz",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200

    async def test_correlation_headers_are_exposed_to_browsers(
        self, client: AsyncClient
    ) -> None:
        response = await client.get("/healthz", headers={"Origin": "http://localhost:5173"})
        assert "X-Request-ID" in response.headers["access-control-expose-headers"]
