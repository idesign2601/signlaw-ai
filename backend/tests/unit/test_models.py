"""ORM metadata sanity checks.

These run without a database. They exist because the schema encodes the rules
that keep citations correct — a chunk always knows its document, page and
section; a document always knows whether it is still in force — and a careless
edit to models.py should fail here rather than in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Index

from app.db import Base
from app.db.enums import (
    ChunkType,
    ConfidenceBand,
    DocType,
    DocumentStatus,
    JobStatus,
    MetadataSource,
    QueryIntent,
    RelationType,
)
from app.db.models import (
    AnswerFeedback,
    BylawRelation,
    ChatMessage,
    Chunk,
    Document,
    Municipality,
    Page,
    Section,
)

EXPECTED_TABLES = {
    # Jurisdiction (Phase 2b added province above municipality)
    "province",
    "municipality",
    # Documents and their content
    "document",
    "bylaw_relation",
    "page",
    "section",
    "chunk",
    "document_table",
    # Ingestion bookkeeping
    "ingestion_job",
    "document_stage_event",
    # Vectors (Phase 3, one table per supported embedding width)
    "embedding_collection",
    "chunk_embedding_384",
    "chunk_embedding_768",
    "chunk_embedding_1024",
    "chunk_embedding_1536",
    # Chat and feedback
    "chat_session",
    "chat_message",
    "answer_feedback",
}


class TestSchemaShape:
    def test_all_tables_are_registered(self) -> None:
        assert set(Base.metadata.tables) == EXPECTED_TABLES

    def test_naming_convention_is_applied(self) -> None:
        # Unnamed constraints must get deterministic names, otherwise Alembic
        # autogenerate produces unreviewable churn.
        assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"


class TestCitationBackbone:
    """Every retrievable unit must carry a complete citation."""

    def test_chunk_records_document_page_and_section(self) -> None:
        columns = Chunk.__table__.columns
        assert not columns["document_id"].nullable
        assert not columns["page_number"].nullable
        assert "section_id" in columns

    def test_section_tree_is_self_referential(self) -> None:
        fk = next(iter(Section.__table__.c.parent_section_id.foreign_keys))
        assert fk.column.table.name == "section"

    def test_section_stores_a_denormalised_path(self) -> None:
        # Citation rendering happens on every answer; it must not need a
        # recursive query.
        assert not Section.__table__.c.full_path.nullable

    def test_chunk_supports_small_to_big_retrieval(self) -> None:
        fk = next(iter(Chunk.__table__.c.parent_chunk_id.foreign_keys))
        assert fk.column.table.name == "chunk"


class TestCurrencyAndLineage:
    """The guard against citing repealed law."""

    def test_document_status_is_not_nullable(self) -> None:
        assert not Document.__table__.c.status.nullable

    def test_document_status_defaults_to_unknown(self) -> None:
        # Never assume a newly ingested document is in force.
        assert Document.__table__.c.status.server_default.arg == DocumentStatus.UNKNOWN.value

    def test_relation_edges_are_unique(self) -> None:
        # Names are matched by substring because the metadata naming convention
        # expands short constraint names at attach time.
        names = [str(c.name) for c in BylawRelation.__table__.constraints if c.name]
        assert any("bylaw_relation_edge" in name for name in names)

    def test_document_cannot_relate_to_itself(self) -> None:
        names = [str(c.name) for c in BylawRelation.__table__.constraints if c.name]
        assert any("no_self_relation" in name for name in names)


class TestIdempotentIngestion:
    def test_document_content_hash_is_unique(self) -> None:
        assert Document.__table__.c.sha256.unique is True

    def test_chunk_content_hash_is_indexed(self) -> None:
        # Lets a re-index skip chunks whose text did not change.
        assert Chunk.__table__.c.content_hash.index is True


class TestAuditability:
    def test_message_persists_the_retrieval_trace(self) -> None:
        assert not ChatMessage.__table__.c.retrieval_trace.nullable

    def test_message_records_the_model_used(self) -> None:
        assert "model_used" in ChatMessage.__table__.columns

    def test_feedback_flags_are_indexed_for_the_review_queue(self) -> None:
        assert AnswerFeedback.__table__.c.flagged_incorrect.index is True


class TestSearchSupport:
    def test_chunk_has_a_full_text_index(self) -> None:
        index_names = {index.name for index in Chunk.__table__.indexes}
        assert "ix_chunk_body_fts" in index_names

    def test_full_text_index_uses_gin(self) -> None:
        fts: Index = next(
            index for index in Chunk.__table__.indexes if index.name == "ix_chunk_body_fts"
        )
        assert fts.dialect_options["postgresql"]["using"] == "gin"

    def test_city_filtering_is_indexed(self) -> None:
        # "Filter by city" must pre-filter, not retrieve globally and discard.
        index_names = {index.name for index in Document.__table__.indexes}
        assert "ix_document_municipality_status" in index_names


class TestCascades:
    @pytest.mark.parametrize(
        ("model", "column"),
        [
            (Page, "document_id"),
            (Section, "document_id"),
            (Chunk, "document_id"),
            (Document, "municipality_id"),
        ],
    )
    def test_deleting_a_parent_cascades(self, model: type, column: str) -> None:
        # Admin "delete document" must not strand orphaned chunks that would
        # keep being retrieved.
        fk = next(iter(model.__table__.c[column].foreign_keys))
        assert fk.ondelete == "CASCADE"

    def test_deleting_a_section_preserves_chunks(self) -> None:
        # Re-parsing sections should not destroy indexed text.
        fk = next(iter(Chunk.__table__.c.section_id.foreign_keys))
        assert fk.ondelete == "SET NULL"


class TestMunicipality:
    def test_slug_is_unique(self) -> None:
        assert Municipality.__table__.c.canonical_slug.unique is True

    def test_aliases_are_stored(self) -> None:
        # Resolves "City of Coquitlam" and "Coquitlam BC" to one municipality.
        assert "aliases" in Municipality.__table__.columns

    def test_permit_url_is_available_for_answers(self) -> None:
        assert "permit_url" in Municipality.__table__.columns


class TestEnums:
    @pytest.mark.parametrize(
        ("enum_cls", "expected"),
        [
            (DocumentStatus, {"in_force", "superseded", "repealed", "unknown"}),
            (RelationType, {"amends", "consolidates", "repeals", "replaces"}),
            (MetadataSource, {"filename", "regex", "llm", "human"}),
            (ChunkType, {"prose", "table", "definition", "schedule", "heading"}),
            (ConfidenceBand, {"high", "medium", "low", "insufficient"}),
            (QueryIntent,
             {"single_city", "multi_city_compare", "keyword", "definition", "out_of_scope"}),
        ],
    )
    def test_enum_values_are_stable(self, enum_cls: type, expected: set[str]) -> None:
        # These values are persisted and appear in API responses; changing one
        # is a breaking change requiring a migration.
        assert {member.value for member in enum_cls} == expected

    def test_doc_type_covers_the_consolidation_cases(self) -> None:
        assert {DocType.BASE, DocType.AMENDMENT, DocType.CONSOLIDATED} <= set(DocType)

    @pytest.mark.parametrize(
        ("status", "terminal"),
        [
            (JobStatus.PENDING, False),
            (JobStatus.RUNNING, False),
            (JobStatus.COMPLETED, True),
            (JobStatus.COMPLETED_WITH_ERRORS, True),
            (JobStatus.FAILED, True),
            (JobStatus.CANCELLED, True),
        ],
    )
    def test_job_terminal_states(self, status: JobStatus, terminal: bool) -> None:
        assert status.is_terminal is terminal
