"""pgvector storage: versioned collections and dimension-routed embeddings.

Embeddings live in Postgres beside the documents, sections, chunks and lineage
they describe. That means a chunk and its vector are written in one transaction
and cannot drift apart, and a filtered search — "in-force bylaws in Coquitlam" —
is a join rather than a fan-out to a second system followed by reconciliation.

**Why one table per dimension.** pgvector needs a fixed column dimension to
build an HNSW index, and different embedding models produce different widths.
Pre-creating a table for each common size makes swapping models a configuration
change rather than a schema migration, while keeping every collection indexable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import SUPPORTED_EMBEDDING_DIMENSIONS
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import CollectionStatus

__all__ = [
    "CHUNK_EMBEDDING_TABLES",
    "ChunkEmbeddingBase",
    "EmbeddingCollection",
    "embedding_model_for",
    "embedding_table_name",
]


def embedding_table_name(dimensions: int) -> str:
    """Physical table holding vectors of a given width."""
    return f"chunk_embedding_{dimensions}"


class EmbeddingCollection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One versioned index over the corpus.

    A collection is identified by three independent axes, because a change to
    any of them invalidates the vectors already stored:

    * **embedding model** — different model, different vector space
    * **chunking version** — different text, different vectors
    * **index version** — an explicit rebuild of the same configuration

    Rebuilding is therefore always additive: the new collection is populated
    while the old one keeps serving, then activated. Nothing is ever partially
    reindexed in place.
    """

    __tablename__ = "embedding_collection"

    # e.g. "signlaw_bge_m3_v1"
    name: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)

    embedding_model: Mapped[str] = mapped_column(String(160), nullable=False)
    # Upstream revision or commit hash, so "BAAI/bge-m3" republished with new
    # weights is distinguishable from the version that produced these vectors.
    embedding_model_revision: Mapped[str | None] = mapped_column(String(80))
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)

    chunking_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    distance_metric: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'cosine'")
    )

    status: Mapped[CollectionStatus] = mapped_column(
        SAEnum(
            CollectionStatus,
            name="collection_status",
            native_enum=True,
            values_callable=lambda e: [member.value for member in e],
        ),
        nullable=False,
        server_default=CollectionStatus.BUILDING.value,
        index=True,
    )

    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint(
            "embedding_model",
            "chunking_version",
            "index_version",
            name="uq_embedding_collection_version",
        ),
        CheckConstraint("dimensions > 0", name="dimensions_positive"),
        CheckConstraint("chunk_count >= 0", name="chunk_count_non_negative"),
    )

    @property
    def table_name(self) -> str:
        return embedding_table_name(self.dimensions)

    @property
    def is_queryable(self) -> bool:
        return self.status is CollectionStatus.ACTIVE


class ChunkEmbeddingBase:
    """Columns shared by every per-dimension embedding table."""

    __abstract__ = True

    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("embedding_collection.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunk.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Hash of the chunk body at embed time. A rebuild that finds an unchanged
    # hash can copy the vector across instead of re-embedding it, which is what
    # makes an index-version bump cheap.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


def _make_embedding_model(dimensions: int) -> type[Any]:
    """Build the ORM class for one vector width.

    Generated rather than hand-written so adding a dimension is a one-line
    change to ``SUPPORTED_EMBEDDING_DIMENSIONS`` plus a migration.
    """
    table = embedding_table_name(dimensions)

    namespace: dict[str, Any] = {
        "__tablename__": table,
        "embedding": mapped_column(Vector(dimensions), nullable=False),
        "collection": relationship("EmbeddingCollection"),
        "__table_args__": (
            Index(f"ix_{table}_chunk_id", "chunk_id"),
            Index(f"ix_{table}_collection_hash", "collection_id", "content_hash"),
        ),
        "__doc__": (
            f"Chunk embeddings of width {dimensions}.\n\n"
            "The HNSW index is created per collection in a migration rather "
            "than declared here, because its build parameters are operational "
            "settings rather than schema."
        ),
    }
    return type(f"ChunkEmbedding{dimensions}", (ChunkEmbeddingBase, Base), namespace)


CHUNK_EMBEDDING_TABLES: dict[int, type[Any]] = {
    dimensions: _make_embedding_model(dimensions) for dimensions in SUPPORTED_EMBEDDING_DIMENSIONS
}


def embedding_model_for(dimensions: int) -> type[Any]:
    """ORM class for a vector width.

    Raises:
        ValueError: No storage table exists for that width.
    """
    try:
        return CHUNK_EMBEDDING_TABLES[dimensions]
    except KeyError:
        raise ValueError(
            f"No pgvector table for {dimensions}-dimensional embeddings. "
            f"Supported: {sorted(CHUNK_EMBEDDING_TABLES)}."
        ) from None
