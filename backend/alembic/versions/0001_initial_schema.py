"""Initial schema.

Creates the full system of record: municipalities, documents and their bylaw
lineage, pages, the section tree, chunks, ingestion jobs, chat history and
answer feedback.

Revision ID: 0001
Revises:
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Enum types --------------------------------------------------------------

DOC_TYPE = postgresql.ENUM(
    "base", "amendment", "consolidated", "schedule", "policy", "unknown", name="doc_type"
)
DOCUMENT_STATUS = postgresql.ENUM(
    "in_force", "superseded", "repealed", "unknown", name="document_status"
)
RELATION_TYPE = postgresql.ENUM(
    "amends", "consolidates", "repeals", "replaces", name="relation_type"
)
METADATA_SOURCE = postgresql.ENUM("filename", "regex", "llm", "human", name="metadata_source")
CHUNK_TYPE = postgresql.ENUM(
    "prose", "table", "definition", "schedule", "heading", name="chunk_type"
)
JOB_STATUS = postgresql.ENUM(
    "pending",
    "running",
    "completed",
    "completed_with_errors",
    "failed",
    "cancelled",
    name="job_status",
)
CHAT_ROLE = postgresql.ENUM("user", "assistant", "system", name="chat_role")
QUERY_INTENT = postgresql.ENUM(
    "single_city",
    "multi_city_compare",
    "keyword",
    "definition",
    "out_of_scope",
    name="query_intent",
)
CONFIDENCE_BAND = postgresql.ENUM(
    "high", "medium", "low", "insufficient", name="confidence_band"
)
FEEDBACK_RATING = postgresql.ENUM(
    "helpful", "not_helpful", "incorrect", name="feedback_rating"
)

ALL_ENUMS = (
    DOC_TYPE,
    DOCUMENT_STATUS,
    RELATION_TYPE,
    METADATA_SOURCE,
    CHUNK_TYPE,
    JOB_STATUS,
    CHAT_ROLE,
    QUERY_INTENT,
    CONFIDENCE_BAND,
    FEEDBACK_RATING,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in ALL_ENUMS:
        enum_type.create(bind, checkfirst=True)

    # Enum columns reference the types created above, so create_type=False
    # prevents a duplicate CREATE TYPE during table creation.
    def enum_col(enum_type: postgresql.ENUM) -> postgresql.ENUM:
        return postgresql.ENUM(*enum_type.enums, name=enum_type.name, create_type=False)

    # --- municipality --------------------------------------------------------
    op.create_table(
        "municipality",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("canonical_slug", sa.String(length=160), nullable=False),
        sa.Column("region", sa.String(length=120), nullable=True),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.String(length=160)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
        sa.Column("website_url", sa.String(length=500), nullable=True),
        sa.Column("permit_url", sa.String(length=500), nullable=True),
        sa.Column("contact_email", sa.String(length=255), nullable=True),
        sa.Column("contact_phone", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_municipality"),
        sa.UniqueConstraint("canonical_slug", name="uq_municipality_canonical_slug"),
    )
    op.create_index("ix_municipality_name", "municipality", ["name"])

    # --- document ------------------------------------------------------------
    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("municipality_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("source_path", sa.String(length=1000), nullable=False),
        sa.Column("blob_path", sa.String(length=1000), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("bylaw_number", sa.String(length=60), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("consolidation_date", sa.Date(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column(
            "doc_type", enum_col(DOC_TYPE), server_default="unknown", nullable=False
        ),
        sa.Column(
            "status", enum_col(DOCUMENT_STATUS), server_default="unknown", nullable=False
        ),
        sa.Column("page_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_scanned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ocr_applied", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "text_quality_score", sa.Float(), server_default=sa.text("1.0"), nullable=False
        ),
        sa.Column("metadata_source", enum_col(METADATA_SOURCE), nullable=True),
        sa.Column(
            "metadata_confidence", sa.Float(), server_default=sa.text("0.0"), nullable=False
        ),
        sa.Column(
            "verified_by_human", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("index_version", sa.Integer(), nullable=True),
        sa.Column("ingestion_error", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["municipality_id"],
            ["municipality.id"],
            name="fk_document_municipality_id_municipality",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document"),
        sa.UniqueConstraint("sha256", name="uq_document_sha256"),
        sa.CheckConstraint(
            "text_quality_score >= 0 AND text_quality_score <= 1",
            name="ck_document_text_quality_score_range",
        ),
        sa.CheckConstraint(
            "metadata_confidence >= 0 AND metadata_confidence <= 1",
            name="ck_document_metadata_confidence_range",
        ),
        sa.CheckConstraint("page_count >= 0", name="ck_document_page_count_non_negative"),
    )
    op.create_index("ix_document_municipality_id", "document", ["municipality_id"])
    op.create_index("ix_document_bylaw_number", "document", ["bylaw_number"])
    op.create_index("ix_document_year", "document", ["year"])
    op.create_index("ix_document_status", "document", ["status"])
    op.create_index("ix_document_index_version", "document", ["index_version"])
    op.create_index(
        "ix_document_municipality_status", "document", ["municipality_id", "status"]
    )
    op.create_index(
        "ix_document_bylaw_lookup", "document", ["municipality_id", "bylaw_number", "year"]
    )

    # --- bylaw_relation ------------------------------------------------------
    op.create_table(
        "bylaw_relation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_document_id", sa.Uuid(), nullable=False),
        sa.Column("child_document_id", sa.Uuid(), nullable=False),
        sa.Column("relation_type", enum_col(RELATION_TYPE), nullable=False),
        sa.Column("detected_by", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.Float(), server_default=sa.text("0.0"), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["parent_document_id"],
            ["document.id"],
            name="fk_bylaw_relation_parent_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["child_document_id"],
            ["document.id"],
            name="fk_bylaw_relation_child_document_id_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_bylaw_relation"),
        sa.UniqueConstraint(
            "parent_document_id",
            "child_document_id",
            "relation_type",
            name="uq_bylaw_relation_edge",
        ),
        sa.CheckConstraint(
            "parent_document_id <> child_document_id",
            name="ck_bylaw_relation_no_self_relation",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_bylaw_relation_confidence_range"
        ),
    )
    op.create_index(
        "ix_bylaw_relation_parent_document_id", "bylaw_relation", ["parent_document_id"]
    )
    op.create_index(
        "ix_bylaw_relation_child_document_id", "bylaw_relation", ["child_document_id"]
    )

    # --- page ----------------------------------------------------------------
    op.create_table(
        "page",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("char_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("has_tables", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("was_ocred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_page_document_id_document",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_page"),
        sa.UniqueConstraint(
            "document_id", "page_number", name="uq_page_document_id_page_number"
        ),
        sa.CheckConstraint("page_number >= 1", name="ck_page_page_number_positive"),
    )

    # --- section -------------------------------------------------------------
    op.create_table(
        "section",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("parent_section_id", sa.Uuid(), nullable=True),
        sa.Column("section_number", sa.String(length=80), nullable=False),
        sa.Column("full_path", sa.String(length=500), nullable=False),
        sa.Column("heading", sa.String(length=500), nullable=True),
        sa.Column("level", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("char_end", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_section_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_section_id"],
            ["section.id"],
            name="fk_section_parent_section_id_section",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_section"),
        sa.CheckConstraint("page_end >= page_start", name="ck_section_page_range_ordered"),
        sa.CheckConstraint("level >= 1", name="ck_section_level_positive"),
    )
    op.create_index("ix_section_parent_section_id", "section", ["parent_section_id"])
    op.create_index("ix_section_document_number", "section", ["document_id", "section_number"])
    op.create_index("ix_section_document_ordinal", "section", ["document_id", "ordinal"])

    # --- chunk ---------------------------------------------------------------
    op.create_table(
        "chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("parent_chunk_id", sa.Uuid(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("chunk_type", enum_col(CHUNK_TYPE), server_default="prose", nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding_model", sa.String(length=160), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("index_version", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_chunk_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["section.id"],
            name="fk_chunk_section_id_section",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_chunk_id"],
            ["chunk.id"],
            name="fk_chunk_parent_chunk_id_chunk",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chunk"),
        sa.CheckConstraint("page_number >= 1", name="ck_chunk_page_number_positive"),
        sa.CheckConstraint("token_count >= 0", name="ck_chunk_token_count_non_negative"),
    )
    op.create_index("ix_chunk_parent_chunk_id", "chunk", ["parent_chunk_id"])
    op.create_index("ix_chunk_content_hash", "chunk", ["content_hash"])
    op.create_index("ix_chunk_index_version", "chunk", ["index_version"])
    op.create_index("ix_chunk_document_ordinal", "chunk", ["document_id", "ordinal"])
    op.create_index("ix_chunk_section", "chunk", ["section_id"])
    op.create_index("ix_chunk_document_page", "chunk", ["document_id", "page_number"])
    # Backs keyword search and the sparse half of hybrid retrieval.
    op.execute(
        "CREATE INDEX ix_chunk_body_fts ON chunk "
        "USING gin (to_tsvector('english'::regconfig, body))"
    )

    # --- ingestion_job -------------------------------------------------------
    op.create_table(
        "ingestion_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", enum_col(JOB_STATUS), server_default="pending", nullable=False),
        sa.Column("source_path", sa.String(length=1000), nullable=False),
        sa.Column("index_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("force_reindex", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("total_documents", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "processed_documents", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("skipped_documents", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_documents", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("total_chunks", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "error_log",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_job"),
        sa.CheckConstraint(
            "total_documents >= 0", name="ck_ingestion_job_total_documents_non_negative"
        ),
    )
    op.create_index("ix_ingestion_job_status", "ingestion_job", ["status"])
    op.create_index("ix_ingestion_job_created", "ingestion_job", ["created_at"])

    # --- chat_session --------------------------------------------------------
    op.create_table(
        "chat_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column(
            "filters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_session"),
    )

    # --- chat_message --------------------------------------------------------
    op.create_table(
        "chat_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("role", enum_col(CHAT_ROLE), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", enum_col(QUERY_INTENT), nullable=True),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("confidence_band", enum_col(CONFIDENCE_BAND), nullable=True),
        sa.Column("abstained", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "retrieval_trace",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("model_used", sa.String(length=160), nullable=True),
        sa.Column("prompt_version", sa.String(length=40), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_session.id"],
            name="fk_chat_message_session_id_chat_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_message"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_chat_message_confidence_range",
        ),
    )
    op.create_index("ix_chat_message_session_id", "chat_message", ["session_id"])
    op.create_index(
        "ix_chat_message_session_created", "chat_message", ["session_id", "created_at"]
    )

    # --- answer_feedback -----------------------------------------------------
    op.create_table(
        "answer_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("rating", enum_col(FEEDBACK_RATING), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "flagged_incorrect", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("reviewed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reviewed_by", sa.String(length=160), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["chat_message.id"],
            name="fk_answer_feedback_message_id_chat_message",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_answer_feedback"),
    )
    op.create_index("ix_answer_feedback_message_id", "answer_feedback", ["message_id"])
    op.create_index(
        "ix_answer_feedback_flagged_incorrect", "answer_feedback", ["flagged_incorrect"]
    )


def downgrade() -> None:
    op.drop_table("answer_feedback")
    op.drop_table("chat_message")
    op.drop_table("chat_session")
    op.drop_table("ingestion_job")
    op.execute("DROP INDEX IF EXISTS ix_chunk_body_fts")
    op.drop_table("chunk")
    op.drop_table("section")
    op.drop_table("page")
    op.drop_table("bylaw_relation")
    op.drop_table("document")
    op.drop_table("municipality")

    bind = op.get_bind()
    for enum_type in reversed(ALL_ENUMS):
        enum_type.drop(bind, checkfirst=True)
