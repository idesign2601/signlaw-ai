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

# Imported at runtime, not under TYPE_CHECKING: FastAPI resolves dependency
# annotations by evaluating them, and a string forward reference it cannot
# resolve fails at application startup rather than at type-check time.
# The module pulls in no inference libraries, so the cost is negligible.
from app.services.rag_service import RagService

__all__ = [
    "DbSession",
    "RagServiceDep",
    "SettingsDep",
    "get_db",
    "get_engine",
    "get_rag_service",
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


def get_rag_service(
    request: Request,
    session: DbSession,
) -> RagService:
    """Assemble the RAG pipeline for one request.

    The expensive collaborators — embedding model, reranker, LLM client — are
    built once during the application lifespan and reused. Only the retriever
    and the trace sink are per-request, because both need the request-scoped
    database session.

    Constructing a provider does not load weights; each loads lazily on first
    use behind its own lock. So startup stays fast and the first question pays
    the model-load cost, which is why the deployment guide sets
    ``OLLAMA_KEEP_ALIVE`` rather than letting it be paid repeatedly.
    """
    from app.rag.retriever import HybridRetriever
    from app.rag.synthesizer import AnswerSynthesizer
    from app.services.trace_store import DatabaseTraceSink

    state = request.app.state
    settings: Settings = state.settings

    embedder = getattr(state, "embedder", None)
    llm = getattr(state, "llm", None)
    if embedder is None or llm is None:  # pragma: no cover — lifespan bug
        raise ConfigurationError("RAG providers are not initialised.")

    return RagService(
        retriever=HybridRetriever(
            session=session,
            embedder=embedder,
            settings=settings.retrieval,
            vector_settings=settings.vector,
            reranker=getattr(state, "reranker", None),
        ),
        synthesizer=AnswerSynthesizer(llm=llm),
        # Every answer is persisted for audit. A legal tool that cannot
        # reconstruct a disputed answer months later is not defensible.
        trace_sink=DatabaseTraceSink(session=session),
    )


RagServiceDep = Annotated[RagService, Depends(get_rag_service)]
