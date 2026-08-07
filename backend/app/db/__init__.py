"""Persistence layer: ORM models, engine and session management.

Importing this package imports every model, which is what makes
``Base.metadata`` complete for Alembic autogenerate.
"""

from app.db.base import Base, metadata
from app.db.models import (
    AnswerFeedback,
    BylawRelation,
    ChatMessage,
    ChatSession,
    Chunk,
    Document,
    DocumentStageEvent,
    DocumentTable,
    IngestionJob,
    Municipality,
    Page,
    Province,
    Section,
)
from app.db.vectors import (
    CHUNK_EMBEDDING_TABLES,
    EmbeddingCollection,
    embedding_model_for,
    embedding_table_name,
)

__all__ = [
    "CHUNK_EMBEDDING_TABLES",
    "AnswerFeedback",
    "Base",
    "BylawRelation",
    "ChatMessage",
    "ChatSession",
    "Chunk",
    "Document",
    "DocumentStageEvent",
    "DocumentTable",
    "EmbeddingCollection",
    "IngestionJob",
    "Municipality",
    "Page",
    "Province",
    "Section",
    "embedding_model_for",
    "embedding_table_name",
    "metadata",
]
