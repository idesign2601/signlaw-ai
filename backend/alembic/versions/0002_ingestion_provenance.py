"""Province hierarchy, resumable stage tracking, structured tables.

Adds:
  * province, with municipality reparented under it
  * document.last_amendment_date, so a citation can state the version date and
    the amendment date separately
  * per-document processing_stage plus an append-only stage event log, so a run
    over hundreds of PDFs resumes per document instead of from zero
  * page extraction method, confidence and geometry
  * document_table, storing headers and rows discretely rather than only as
    flattened markdown

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PROCESSING_STAGE = postgresql.ENUM(
    "uploaded",
    "extracted",
    "ocr_completed",
    "tables_extracted",
    "metadata_detected",
    "sections_parsed",
    "chunked",
    "embedded",
    "indexed",
    "failed",
    name="processing_stage",
)

EXTRACTION_METHOD = postgresql.ENUM(
    "text_layer", "ocr", "mixed", "failed", name="extraction_method"
)

NEW_ENUMS = (PROCESSING_STAGE, EXTRACTION_METHOD)


def _existing(enum_type: postgresql.ENUM) -> postgresql.ENUM:
    return postgresql.ENUM(*enum_type.enums, name=enum_type.name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in NEW_ENUMS:
        enum_type.create(bind, checkfirst=True)

    # --- province ------------------------------------------------------------
    op.create_table(
        "province",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("code", sa.String(length=4), nullable=False),
        sa.Column(
            "country_code", sa.String(length=2), server_default=sa.text("'CA'"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_province"),
        sa.UniqueConstraint("name", name="uq_province_name"),
        sa.UniqueConstraint("code", name="uq_province_code"),
    )

    # British Columbia is the corpus, so seed it here rather than leaving the
    # table empty and every municipality unparented on first run.
    op.execute(
        "INSERT INTO province (id, name, code, country_code) "
        "VALUES (gen_random_uuid(), 'British Columbia', 'BC', 'CA')"
    )

    # --- municipality --------------------------------------------------------
    op.add_column("municipality", sa.Column("province_id", sa.Uuid(), nullable=True))
    op.add_column(
        "municipality", sa.Column("classification", sa.String(length=40), nullable=True)
    )
    op.create_foreign_key(
        "fk_municipality_province_id_province",
        "municipality",
        "province",
        ["province_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_municipality_province_id", "municipality", ["province_id"])
    op.create_unique_constraint(
        "uq_municipality_province_name", "municipality", ["province_id", "name"]
    )
    # Existing rows predate the hierarchy and are all BC.
    op.execute(
        "UPDATE municipality SET province_id = (SELECT id FROM province WHERE code = 'BC') "
        "WHERE province_id IS NULL"
    )

    # --- document ------------------------------------------------------------
    op.add_column("document", sa.Column("last_amendment_date", sa.Date(), nullable=True))
    op.add_column(
        "document",
        sa.Column(
            "processing_stage",
            _existing(PROCESSING_STAGE),
            server_default="uploaded",
            nullable=False,
        ),
    )
    op.add_column(
        "document", sa.Column("stage_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "document", sa.Column("failed_stage", _existing(PROCESSING_STAGE), nullable=True)
    )
    op.add_column(
        "document",
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index("ix_document_processing_stage", "document", ["processing_stage"])

    # --- page ----------------------------------------------------------------
    op.add_column(
        "page",
        sa.Column(
            "extraction_method",
            _existing(EXTRACTION_METHOD),
            server_default="text_layer",
            nullable=False,
        ),
    )
    op.add_column(
        "page",
        sa.Column(
            "extraction_confidence",
            sa.Float(),
            server_default=sa.text("1.0"),
            nullable=False,
        ),
    )
    op.add_column("page", sa.Column("width", sa.Float(), nullable=True))
    op.add_column("page", sa.Column("height", sa.Float(), nullable=True))
    op.add_column(
        "page", sa.Column("rotation", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    # Raw SQL rather than op.create_check_constraint: the metadata naming
    # convention is `ck_%(table_name)s_%(constraint_name)s`, so passing a name
    # that already starts with `ck_page_` risks Alembic producing
    # `ck_page_ck_page_...` on create while downgrade() drops the name written
    # here. Spelling the DDL out makes create and drop provably symmetric.
    op.execute(
        "ALTER TABLE page ADD CONSTRAINT ck_page_extraction_confidence_range "
        "CHECK (extraction_confidence >= 0 AND extraction_confidence <= 1)"
    )

    # --- document_table ------------------------------------------------------
    op.create_table(
        "document_table",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("caption", sa.String(length=500), nullable=True),
        sa.Column(
            "headers",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "rows",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("column_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("markdown", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("bbox", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_document_table_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"],
            ["section.id"],
            name="fk_document_table_section_id_section",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_table"),
        # Bare name: the target metadata's naming convention prepends
        # `ck_document_table_`. See the note in 0001.
        sa.CheckConstraint("page_number >= 1", name="page_number_positive"),
    )
    op.create_index("ix_document_table_document_id", "document_table", ["document_id"])
    op.create_index("ix_document_table_section_id", "document_table", ["section_id"])
    op.create_index(
        "ix_document_table_document_page", "document_table", ["document_id", "page_number"]
    )

    # --- document_stage_event ------------------------------------------------
    op.create_table(
        "document_stage_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("stage", _existing(PROCESSING_STAGE), nullable=False),
        sa.Column("succeeded", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["document.id"],
            name="fk_document_stage_event_document_id_document",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["ingestion_job.id"],
            name="fk_document_stage_event_job_id_ingestion_job",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_stage_event"),
    )
    op.create_index("ix_document_stage_event_document_id", "document_stage_event", ["document_id"])
    op.create_index("ix_document_stage_event_job_id", "document_stage_event", ["job_id"])
    op.create_index(
        "ix_document_stage_event_doc_created",
        "document_stage_event",
        ["document_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("document_stage_event")
    op.drop_table("document_table")

    # IF EXISTS so a downgrade still completes against a database where an
    # earlier run created the constraint under a convention-mangled name.
    op.execute(
        "ALTER TABLE page DROP CONSTRAINT IF EXISTS ck_page_extraction_confidence_range"
    )
    for column in ("rotation", "height", "width", "extraction_confidence", "extraction_method"):
        op.drop_column("page", column)

    op.drop_index("ix_document_processing_stage", table_name="document")
    for column in (
        "attempt_count",
        "failed_stage",
        "stage_updated_at",
        "processing_stage",
        "last_amendment_date",
    ):
        op.drop_column("document", column)

    op.drop_constraint("uq_municipality_province_name", "municipality", type_="unique")
    op.drop_index("ix_municipality_province_id", table_name="municipality")
    op.drop_constraint(
        "fk_municipality_province_id_province", "municipality", type_="foreignkey"
    )
    op.drop_column("municipality", "classification")
    op.drop_column("municipality", "province_id")

    op.drop_table("province")

    bind = op.get_bind()
    for enum_type in reversed(NEW_ENUMS):
        enum_type.drop(bind, checkfirst=True)
