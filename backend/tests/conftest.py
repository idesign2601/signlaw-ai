"""Shared pytest fixtures.

Unit and e2e tests run without Postgres, Redis or any model server. The database
engine is replaced with a stub whose connectivity probe is controlled per test,
so health, error-handling and auth behaviour are all exercised without
infrastructure.

Integration tests are the deliberate exception: they exist to verify the real
schema against a real database, so they read the real configuration. See
:func:`isolated_env`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Every setting this project reads, so a developer's real .env never leaks into
# a test run and changes its outcome.
_MANAGED_ENV_PREFIXES = (
    "APP_",
    "ENVIRONMENT",
    "DEBUG",
    "API_PREFIX",
    "HOST",
    "PORT",
    "DB__",
    "REDIS__",
    "LLM__",
    "EMBEDDING__",
    "VECTOR__",
    "INGESTION__",
    "RETRIEVAL__",
    "SECURITY__",
    "OBSERVABILITY__",
)


@pytest.fixture(autouse=True)
def isolated_env(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Strip inherited configuration and pin a known-good baseline.

    Integration tests are exempt. They connect to a real Postgres, so pinning a
    fake ``DB__PASSWORD`` here would make every one of them fail authentication
    — the environment and ``.env`` are exactly what they are meant to exercise.

    The settings cache is still cleared on both sides regardless, because
    ``get_settings`` is memoised process-wide and a unit test running earlier in
    the session would otherwise leave the stub configuration in place.
    """
    from app.core.config import get_settings

    if request.node.get_closest_marker("integration"):
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()
        return

    for key in list(os.environ):
        if key.startswith(_MANAGED_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("ENVIRONMENT", "local")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("DB__HOST", "localhost")
    monkeypatch.setenv("DB__PASSWORD", "test-password")
    monkeypatch.setenv("OBSERVABILITY__LOG_LEVEL", "WARNING")

    from app.core.config import Settings

    # A developer's local .env must not change test outcomes.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings_factory() -> Any:
    """Build a Settings object with overrides, bypassing the env and cache."""
    from app.core.config import Settings

    def _factory(**overrides: Any) -> Settings:
        # _env_file=None so a developer's .env cannot influence the result.
        return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]

    return _factory


class StubEngine:
    """Stands in for an AsyncEngine in tests that never touch Postgres."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


@pytest.fixture
def stub_engine() -> StubEngine:
    return StubEngine()


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch, stub_engine: StubEngine
) -> Generator[FastAPI, None, None]:
    """A fully wired application with the database replaced by a stub."""
    import app.db.session as session_module
    import app.main as main_module
    from app.core.config import get_settings

    monkeypatch.setattr(main_module, "create_engine", lambda _settings: stub_engine)
    monkeypatch.setattr(main_module, "create_session_factory", lambda _engine: None)
    monkeypatch.setattr(main_module, "dispose_engine", _dispose_stub)

    async def fake_ping(engine: Any) -> bool:
        return bool(getattr(engine, "healthy", False))

    monkeypatch.setattr(session_module, "ping", fake_ping)
    # health.py imported `ping` by name, so patch that reference too.
    import app.api.v1.health as health_module

    monkeypatch.setattr(health_module, "ping", fake_ping)

    application = main_module.create_app(get_settings())
    yield application


async def _dispose_stub(engine: Any) -> None:
    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client bound to the app, with lifespan startup/shutdown executed."""
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
