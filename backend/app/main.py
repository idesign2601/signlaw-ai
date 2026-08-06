"""Application entry point.

Wires configuration, logging, middleware, error handling, the database engine
and the versioned router into a FastAPI application.

Startup deliberately fails fast: if the configuration is invalid the process
exits rather than serving requests that would fail one by one later.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.errors import register_exception_handlers
from app.api.middleware import RequestContextMiddleware
from app.api.v1 import api_router
from app.api.v1.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import create_engine, create_session_factory, dispose_engine
from app.rag.collections import CollectionSpec

__all__ = ["app", "create_app"]

logger = get_logger(__name__)

DESCRIPTION = """
Citation-first retrieval over British Columbia municipal sign bylaws.

Every answer is grounded in indexed bylaw text and carries its source document,
page number, section number and a confidence score. Where the corpus does not
support an answer, the system abstains rather than guessing.

**Informational only — not legal advice.** Verify against the municipality
before applying for a permit or fabricating signage.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage resources that live as long as the process."""
    settings: Settings = app.state.settings

    # The collection name depends on the embedding model, not just the vector
    # settings, so it is derived through CollectionSpec — the single place that
    # knows the naming scheme.
    collection = CollectionSpec(
        prefix=settings.vector.collection_prefix,
        embedding_model=settings.embedding.model,
        dimensions=settings.embedding.dimensions,
        index_version=settings.vector.index_version,
        chunking_version=settings.vector.chunking_version,
        distance_metric=settings.vector.distance_metric,
    )

    logger.info(
        "application_starting",
        service=settings.app_name,
        version=settings.version,
        environment=settings.environment.value,
        llm_provider=settings.llm.provider.value,
        llm_model=settings.llm.model,
        embedding_provider=settings.embedding.provider.value,
        embedding_model=settings.embedding.model,
        vector_collection=collection.name,
    )

    engine = create_engine(settings)
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    try:
        yield
    finally:
        logger.info("application_stopping")
        await dispose_engine(engine)
        logger.info("application_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Accepts an explicit ``settings`` object so tests can construct an app with a
    tailored configuration without mutating the environment.
    """
    settings = settings or get_settings()
    configure_logging(settings.observability)

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        # API documentation is disabled in production: the schema describes
        # admin routes and is not something to publish by default.
        docs_url=None if settings.environment.is_production else "/docs",
        redoc_url=None if settings.environment.is_production else "/redoc",
        openapi_url=None if settings.environment.is_production else "/openapi.json",
    )
    app.state.settings = settings

    # Middleware executes in reverse registration order, so the request-context
    # middleware is registered last and therefore runs first — every downstream
    # log line and error body then carries the correlation ID.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.security.cors_origins,
        allow_credentials=settings.security.cors_allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-ms"],
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)

    return app


app = create_app()
