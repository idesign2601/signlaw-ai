"""pgvector embeddings with versioned collections.

Embeddings live in Postgres beside the relational data they describe, so a
chunk and its vector are written in one transaction and a filtered search is a
join rather than a cross-system reconciliation.

One physical table per vector width, because pgvector needs a fixed column
dimension to build an HNSW index. Pre-creating the common widths means changing
embedding model is a configuration change rather than a migration.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Must match app.core.config.SUPPORTED_EMBEDDING_DIMENSIONS.
DIMENSIONS: tuple[int, ...] = (384, 768, 1024, 1536)

COLLECTION_STATUS = postgresql.ENUM(
    "building", "active", "retired", "failed", name="collection_status"
)

# HNSW build parameters. m controls graph connectivity, ef_construction the
# build-time candidate list; both trade build time for recall.
HNSW_M = 16
HNSW_EF_CONSTRUCTION = 64


def upgrade() -> None:
    bind = op.get_bind()

    # Requires the pgvector extension to be installed in the image. The
    # pgvector/pgvector Postgres image ships it; stock postgres:16 does not.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    COLLECTION_STATUS.create(bind, checkfirst=True)

    op.create_table(
        "embedding_collection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("embedding_model", sa.String(length=160), nullable=False),
        sa.Column("embedding_model_revision", sa.String(length=80), nullable=True),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("chunking_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("index_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "distance_metric",
            sa.String(length=16),
            server_default=sa.text("'cosine'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                *COLLECTION_STATUS.enums, name="collection_status", create_type=False
            ),
            server_default="building",
            nullable=False,
        ),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_embedding_collection"),
        sa.UniqueConstraint("name", name="uq_embedding_collection_name"),
        sa.UniqueConstraint(
            "embedding_model",
            "chunking_version",
            "index_version",
            name="uq_embedding_collection_version",
        ),
        sa.CheckConstraint("dimensions > 0", name="ck_embedding_collection_dimensions_positive"),
        sa.CheckConstraint(
            "chunk_count >= 0", name="ck_embedding_collection_chunk_count_non_negative"
        ),
    )
    op.create_index("ix_embedding_collection_status", "embedding_collection", ["status"])

    # At most one active collection: retrieval must never have to choose.
    op.execute(
        "CREATE UNIQUE INDEX uq_embedding_collection_single_active "
        "ON embedding_collection (status) WHERE status = 'active'"
    )

    for dimensions in DIMENSIONS:
        table = f"chunk_embedding_{dimensions}"

        op.create_table(
            table,
            sa.Column("collection_id", sa.Uuid(), nullable=False),
            sa.Column("chunk_id", sa.Uuid(), nullable=False),
            sa.Column("embedding", Vector(dimensions), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(
                ["collection_id"],
                ["embedding_collection.id"],
                name=f"fk_{table}_collection_id_embedding_collection",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["chunk_id"],
                ["chunk.id"],
                name=f"fk_{table}_chunk_id_chunk",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("collection_id", "chunk_id", name=f"pk_{table}"),
        )

        op.create_index(f"ix_{table}_chunk_id", table, ["chunk_id"])
        op.create_index(f"ix_{table}_collection_hash", table, ["collection_id", "content_hash"])

        # HNSW rather than IVFFlat: no training step, and recall stays stable as
        # the corpus grows, which matters when bylaws are added incrementally.
        for metric, ops_class in (
            ("cosine", "vector_cosine_ops"),
            ("l2", "vector_l2_ops"),
        ):
            op.execute(
                f"CREATE INDEX ix_{table}_hnsw_{metric} ON {table} "
                f"USING hnsw (embedding {ops_class}) "
                f"WITH (m = {HNSW_M}, ef_construction = {HNSW_EF_CONSTRUCTION})"
            )

    # Records which collection a chunk currently belongs to, so a rebuild can
    # find chunks whose text is unchanged and copy their vectors forward.
    op.create_index(
        "ix_chunk_content_hash_document", "chunk", ["content_hash", "document_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_chunk_content_hash_document", table_name="chunk")

    for dimensions in reversed(DIMENSIONS):
        op.drop_table(f"chunk_embedding_{dimensions}")

    op.execute("DROP INDEX IF EXISTS uq_embedding_collection_single_active")
    op.drop_table("embedding_collection")

    COLLECTION_STATUS.drop(op.get_bind(), checkfirst=True)
    # The vector extension is left installed: other schemas may rely on it, and
    # dropping it would cascade to any remaining vector columns.
