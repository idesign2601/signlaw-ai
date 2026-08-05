"""Async database engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.logging import get_logger

__all__ = [
    "create_engine",
    "create_session_factory",
    "dispose_engine",
    "ping",
    "session_scope",
]

logger = get_logger(__name__)


def create_engine(settings: Settings) -> AsyncEngine:
    """Create the async engine.

    One engine per process, owned by the application lifespan.
    """
    engine = create_async_engine(
        settings.db.async_url,
        echo=settings.db.echo or settings.observability.log_sql,
        pool_size=settings.db.pool_size,
        max_overflow=settings.db.max_overflow,
        pool_timeout=settings.db.pool_timeout_s,
        pool_recycle=settings.db.pool_recycle_s,
        pool_pre_ping=settings.db.pool_pre_ping,
        connect_args={
            "timeout": settings.db.connect_timeout_s,
            "server_settings": {"application_name": "signlaw-api"},
        },
    )
    logger.info("database_engine_created", url=settings.db.safe_url, pool_size=settings.db.pool_size)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the session factory bound to an engine.

    ``expire_on_commit=False`` so ORM objects stay usable after the request's
    transaction commits and the session closes.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession, None]:
    """Transactional scope: commit on success, roll back on any exception.

    Used outside the request cycle — ingestion worker tasks and CLI entry
    points. Request handlers use the ``get_db`` dependency instead.
    """
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def ping(engine: AsyncEngine) -> bool:
    """Return whether the database answers a trivial query.

    Never raises: readiness checks report status rather than failing.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — health checks must not propagate
        logger.warning("database_ping_failed", error=str(exc), error_type=type(exc).__name__)
        return False
    return True


async def dispose_engine(engine: AsyncEngine) -> None:
    """Close all pooled connections during shutdown."""
    await engine.dispose()
    logger.info("database_engine_disposed")
