"""Shared FastAPI dependencies.

Long-lived resources — the database engine and session factory — are created
once in the application lifespan and stored on ``app.state``. Dependencies read
them from there rather than constructing their own, so a request never opens a
second connection pool.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError

__all__ = [
    "DbSession",
    "SettingsDep",
    "get_db",
    "get_engine",
    "get_session_factory",
]


def get_engine(request: Request) -> AsyncEngine:
    """Return the process-wide async engine."""
    engine: AsyncEngine | None = getattr(request.app.state, "engine", None)
    if engine is None:  # pragma: no cover — indicates a lifespan bug
        raise ConfigurationError("Database engine is not initialised.")
    return engine


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory."""
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:  # pragma: no cover — indicates a lifespan bug
        raise ConfigurationError("Database session factory is not initialised.")
    return factory


async def get_db(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncGenerator[AsyncSession, None]:
    """Yield a request-scoped session.

    Commits when the handler returns normally, rolls back on any exception, so
    handlers never manage transactions by hand.
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


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[AsyncSession, Depends(get_db)]
