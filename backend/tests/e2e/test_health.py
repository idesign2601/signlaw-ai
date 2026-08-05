"""Health and readiness endpoints over HTTP."""

from __future__ import annotations

from httpx import AsyncClient

from tests.conftest import StubEngine


class TestLiveness:
    async def test_returns_ok(self, client: AsyncClient) -> None:
        response = await client.get("/healthz")
        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "ok"
        assert body["service"]
        assert body["version"]
        assert body["environment"] == "local"

    async def test_ignores_database_health(
        self, client: AsyncClient, stub_engine: StubEngine
    ) -> None:
        # Liveness must not fail on a dependency outage, or the orchestrator
        # will restart a perfectly healthy process.
        stub_engine.healthy = False
        assert (await client.get("/healthz")).status_code == 200

    async def test_response_carries_correlation_headers(self, client: AsyncClient) -> None:
        response = await client.get("/healthz")
        assert response.headers["X-Request-ID"]
        assert float(response.headers["X-Response-Time-ms"]) >= 0

    async def test_inbound_request_id_is_honoured(self, client: AsyncClient) -> None:
        response = await client.get("/healthz", headers={"X-Request-ID": "trace-me"})
        assert response.headers["X-Request-ID"] == "trace-me"


class TestReadiness:
    async def test_ready_when_database_responds(self, client: AsyncClient) -> None:
        response = await client.get("/readyz")
        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "ok"
        assert [component["name"] for component in body["components"]] == ["database"]
        assert body["components"][0]["status"] == "ok"

    async def test_503_when_database_is_down(
        self, client: AsyncClient, stub_engine: StubEngine
    ) -> None:
        stub_engine.healthy = False
        response = await client.get("/readyz")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["components"][0]["status"] == "unavailable"
        assert body["components"][0]["detail"]

    async def test_latency_is_reported(self, client: AsyncClient) -> None:
        component = (await client.get("/readyz")).json()["components"][0]
        assert component["latency_ms"] >= 0

    async def test_healthy_component_has_no_detail(self, client: AsyncClient) -> None:
        assert (await client.get("/readyz")).json()["components"][0]["detail"] is None
