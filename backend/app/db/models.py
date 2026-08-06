"""SQLAlchemy ORM models — the system of record.

Postgres holds everything: documents, bylaw lineage, the section tree, chunk
text, ingestion jobs, chat history and feedback. ChromaDB holds only vectors and
a thin slice of filter metadata, and is fully rebuildable from these tables.

That split is deliberate. Re-chunking or swapping the embedding model then costs
one embedding pass instead of re-OCRing hundreds of scanned PDFs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.enums import (
    ChatRole,
    ChunkType,
    ConfidenceBand,
    DocType,
    DocumentStatus,
    ExtractionMethod,
    FeedbackRating,
    JobStatus,
    MetadataSource,
    ProcessingStage,
    QueryIntent,
    RelationType,
)

__all__ = [
    "AnswerFeedback",
    "BylawRelation",
    "ChatMessage",
    "ChatSession",
    "Chunk",
    "Document",
    "DocumentStageEvent",
    "DocumentTable",
    "IngestionJob",
    "Municipality",
    "Page",
    "Province",
    "Section",
]


def _pg_enum(enum_cls: type, name: str) -> SAEnum:
    """Build a native Postgres enum that stores the member *values*."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        create_type=True,
        values_callable=lambda e: [member.value for member in e],
    )


# Shared instance: `processing_stage` is used by two columns on `document`, and
# a second SAEnum object with the same type name would make SQLAlchemy try to
# create the Postgres type twice.
PROCESSING_STAGE_ENUM = _pg_enum(ProcessingStage, "processing_stage")


# =============================================================================
# Jurisdiction: Province -> Municipality -> Document
# =============================================================================


class Province(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A province or territory.

    The corpus is British Columbia today, but the hierarchy is explicit so that
    adding Alberta or Ontario later is data entry rather than a migration —
    and so a municipality name that exists in several provinces (Victoria,
    Langley, Delta) can never be ambiguous.
    """

    __tablename__ = "province"

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    code: Mapped[str] = mapped_column(String(4), nullable=False, unique=True)
    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default=text("'CA'")
    )

    municipalities: Mapped[list[Municipality]] = relationship(
        back_populates="province", cascade="all, delete-orphan", passive_deletes=True
    )


class Municipality(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A municipality or regional district that publishes sign bylaws."""

    __tablename__ = "municipality"

    province_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("province.id", ondelete="CASCADE"), index=True
    )
    # Incorporated status: city, district, town, village, regional_district.
    classification: Mapped[str | None] = mapped_column(String(40))

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    canonical_slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    region: Mapped[str | None] = mapped_column(String(120))

    # Alternative spellings seen in filenames and document headers, e.g.
    # {"City of Coquitlam", "Coquitlam BC"}. Used to resolve a city named in a
    # question to a canonical municipality.
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(160)), nullable=False, server_default=text("'{}'::varchar[]")
    )

    website_url: Mapped[str | None] = mapped_column(String(500))
    # Surfaced alongside answers: the practical next step for the reader.
    permit_url: Mapped[str | None] = mapped_column(String(500))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_phone: Mapped[str | None] = mapped_column(String(50))

    province: Mapped[Province | None] = relationship(back_populates="municipalities")
    documents: Mapped[list[Document]] = relationship(
        back_populates="municipality", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_municipality_name", "name"),
        UniqueConstraint("province_id", "name", name="uq_municipality_province_name"),
    )


# =============================================================================
# Document
# =============================================================================


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One source PDF."""

    __tablename__ = "document"

    municipality_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("municipality.id", ondelete="CASCADE"), index=True
    )

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    blob_path: Mapped[str | None] = mapped_column(String(1000))
    # Content hash makes re-running ingestion over the same folder idempotent.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    title: Mapped[str | None] = mapped_column(String(500))
    bylaw_number: Mapped[str | None] = mapped_column(String(60), index=True)
    year: Mapped[int | None] = mapped_column(Integer, index=True)
    # "Consolidated to <date>" — the strongest available currency signal.
    # --- version and currency -------------------------------------------
    # "Consolidated to <date>" — the version of the text in this PDF.
    consolidation_date: Mapped[date | None] = mapped_column(Date)
    # When the bylaw came into force.
    effective_date: Mapped[date | None] = mapped_column(Date)
    # Date of the most recent amendment reflected in this document. Distinct
    # from consolidation_date: a consolidation may post-date the amendment it
    # incorporates, and a citation must be able to state both.
    last_amendment_date: Mapped[date | None] = mapped_column(Date)

    doc_type: Mapped[DocType] = mapped_column(
        _pg_enum(DocType, "doc_type"), nullable=False, server_default=DocType.UNKNOWN.value
    )
    status: Mapped[DocumentStatus] = mapped_column(
        _pg_enum(DocumentStatus, "document_status"),
        nullable=False,
        server_default=DocumentStatus.UNKNOWN.value,
        index=True,
    )

    page_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_scanned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ocr_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # 0.0-1.0. Drives the confidence penalty applied to answers citing this doc.
    text_quality_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("1.0")
    )

    metadata_source: Mapped[MetadataSource | None] = mapped_column(
        _pg_enum(MetadataSource, "metadata_source")
    )
    metadata_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.0")
    )
    # Set when an operator corrects detected metadata in the admin UI. Human
    # corrections are never overwritten by a later automated pass.
    verified_by_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # --- resumable ingestion --------------------------------------------
    # How far this document has progressed. A run that dies on document 120
    # resumes each document from its own last completed stage.
    processing_stage: Mapped[ProcessingStage] = mapped_column(
        PROCESSING_STAGE_ENUM,
        nullable=False,
        server_default=ProcessingStage.UPLOADED.value,
        index=True,
    )
    stage_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Stage that raised, so a retry knows where to pick up.
    failed_stage: Mapped[ProcessingStage | None] = mapped_column(PROCESSING_STAGE_ENUM)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_version: Mapped[int | None] = mapped_column(Integer, index=True)
    ingestion_error: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    municipality: Mapped[Municipality | None] = relationship(back_populates="documents")
    tables: Mapped[list[DocumentTable]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    stage_events: Mapped[list[DocumentStageEvent]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    pages: Mapped[list[Page]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    sections: Mapped[list[Section]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(
            "text_quality_score >= 0 AND text_quality_score <= 1",
            name="text_quality_score_range",
        ),
        CheckConstraint(
            "metadata_confidence >= 0 AND metadata_confidence <= 1",
            name="metadata_confidence_range",
        ),
        CheckConstraint("page_count >= 0", name="page_count_non_negative"),
        Index("ix_document_municipality_status", "municipality_id", "status"),
        Index("ix_document_bylaw_lookup", "municipality_id", "bylaw_number", "year"),
    )


class BylawRelation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A directed edge in a bylaw lineage.

    ``parent`` is the bylaw being acted upon; ``child`` is the bylaw doing the
    acting. So an amending bylaw is the *child* of the base bylaw it amends.

    This table plus :attr:`Document.status` is what prevents the system citing
    repealed text with confidence.
    """

    __tablename__ = "bylaw_relation"

    parent_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    child_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[RelationType] = mapped_column(
        _pg_enum(RelationType, "relation_type"), nullable=False
    )
    # How the edge was inferred, e.g. "regex:amends_clause" or "human".
    detected_by: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.0"))
    evidence: Mapped[str | None] = mapped_column(Text)

    parent_document: Mapped[Document] = relationship(foreign_keys=[parent_document_id])
    child_document: Mapped[Document] = relationship(foreign_keys=[child_document_id])

    __table_args__ = (
        UniqueConstraint(
            "parent_document_id",
            "child_document_id",
            "relation_type",
            name="uq_bylaw_relation_edge",
        ),
        CheckConstraint(
            "parent_document_id <> child_document_id", name="no_self_relation"
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )


# =============================================================================
# Page and section
# =============================================================================


class Page(UUIDPrimaryKeyMixin, Base):
    """Extracted text for a single PDF page.

    Retained after chunking so the admin document viewer can render source text,
    and so re-chunking never requires re-extraction or re-OCR.
    """

    __tablename__ = "page"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    has_tables: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    was_ocred: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    ocr_confidence: Mapped[float | None] = mapped_column(Float)

    extraction_method: Mapped[ExtractionMethod] = mapped_column(
        _pg_enum(ExtractionMethod, "extraction_method"),
        nullable=False,
        server_default=ExtractionMethod.TEXT_LAYER.value,
    )
    # 0.0-1.0 composite of character density, replacement-character rate and
    # dictionary hit rate. Drives the OCR decision and the citation penalty.
    extraction_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("1.0")
    )
    # Page geometry in PDF points, needed to map a stored bbox back onto a
    # rendered page for highlight-on-open in the document viewer.
    width: Mapped[float | None] = mapped_column(Float)
    height: Mapped[float | None] = mapped_column(Float)
    rotation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    document: Mapped[Document] = relationship(back_populates="pages")

    __table_args__ = (
        UniqueConstraint("document_id", "page_number", name="uq_page_document_id_page_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint(
            "extraction_confidence >= 0 AND extraction_confidence <= 1",
            name="extraction_confidence_range",
        ),
    )


class DocumentTable(UUIDPrimaryKeyMixin, Base):
    """A table extracted from a page, stored with its structure intact.

    Kept separate from prose because sign bylaws express most numeric limits as
    tables — maximum area by zone and sign type, height by street
    classification. Flattened into paragraphs the row/column association is
    lost, and the model will confidently pair the wrong number with the wrong
    zone. Headers and rows are stored discretely so a future renderer can
    rebuild the grid rather than re-parsing markdown.
    """

    __tablename__ = "document_table"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The section this table sits inside, which is what a citation to it names.
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("section.id", ondelete="SET NULL"), index=True
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    caption: Mapped[str | None] = mapped_column(String(500))
    headers: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    # Rows as a JSONB array of arrays, preserving ragged rows verbatim rather
    # than padding them and inventing empty cells.
    rows: Mapped[list[list[str]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    column_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Markdown rendering handed to the model at answer time.
    markdown: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # Location on the page in PDF points: x0, y0, x1, y1.
    bbox: Mapped[list[float] | None] = mapped_column(JSONB)

    document: Mapped[Document] = relationship(back_populates="tables")
    section: Mapped[Section | None] = relationship()

    __table_args__ = (
        Index("ix_document_table_document_page", "document_id", "page_number"),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
    )


class DocumentStageEvent(UUIDPrimaryKeyMixin, Base):
    """An append-only record of every ingestion stage transition.

    Two jobs: it makes a stalled 500-document run diagnosable at a glance, and
    it gives per-stage timings so the slow part of the pipeline is measured
    rather than guessed at.
    """

    __tablename__ = "document_stage_event"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ingestion_job.id", ondelete="SET NULL"), index=True
    )
    stage: Mapped[ProcessingStage] = mapped_column(PROCESSING_STAGE_ENUM, nullable=False)
    succeeded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="stage_events")

    __table_args__ = (Index("ix_document_stage_event_doc_created", "document_id", "created_at"),)


class Section(UUIDPrimaryKeyMixin, Base):
    """A node in the bylaw's section hierarchy — the citation backbone.

    Self-referential, so ``Part 5 > 5.3 > 5.3(b)`` is a real tree. ``full_path``
    is denormalised because citation rendering happens on every answer and must
    not require a recursive query.
    """

    __tablename__ = "section"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    parent_section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("section.id", ondelete="CASCADE"), index=True
    )

    section_number: Mapped[str] = mapped_column(String(80), nullable=False)
    full_path: Mapped[str] = mapped_column(String(500), nullable=False)
    heading: Mapped[str | None] = mapped_column(String(500))
    level: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # Position among siblings, so document order can be restored after a query.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    char_end: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    document: Mapped[Document] = relationship(back_populates="sections")
    parent: Mapped[Section | None] = relationship(
        back_populates="children", remote_side="Section.id"
    )
    children: Mapped[list[Section]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", passive_deletes=True
    )
    chunks: Mapped[list[Chunk]] = relationship(back_populates="section")

    __table_args__ = (
        Index("ix_section_document_number", "document_id", "section_number"),
        Index("ix_section_document_ordinal", "document_id", "ordinal"),
        CheckConstraint("page_end >= page_start", name="page_range_ordered"),
        CheckConstraint("level >= 1", name="level_positive"),
    )


# =============================================================================
# Chunk
# =============================================================================


class Chunk(UUIDPrimaryKeyMixin, Base):
    """A retrievable unit of text.

    Chunks never cross a section boundary, so the section number attached here
    is always the correct citation for the text it contains.
    """

    __tablename__ = "chunk"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=False
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("section.id", ondelete="SET NULL")
    )
    # Small-to-big retrieval: match a precise child, then hand the model the
    # whole parent section for context.
    parent_chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chunk.id", ondelete="SET NULL"), index=True
    )

    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int | None] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    body: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunk_type: Mapped[ChunkType] = mapped_column(
        _pg_enum(ChunkType, "chunk_type"), nullable=False, server_default=ChunkType.PROSE.value
    )
    # Hash of body + chunking parameters: lets a re-index skip unchanged chunks.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    embedding_model: Mapped[str | None] = mapped_column(String(160))
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    index_version: Mapped[int | None] = mapped_column(Integer, index=True)

    document: Mapped[Document] = relationship(back_populates="chunks")
    section: Mapped[Section | None] = relationship(back_populates="chunks")
    parent_chunk: Mapped[Chunk | None] = relationship(remote_side="Chunk.id")

    __table_args__ = (
        Index("ix_chunk_document_ordinal", "document_id", "ordinal"),
        Index("ix_chunk_section", "section_id"),
        Index("ix_chunk_document_page", "document_id", "page_number"),
        # Lets a rebuild find chunks whose text is unchanged and copy their
        # vectors forward instead of re-embedding them.
        Index("ix_chunk_content_hash_document", "content_hash", "document_id"),
        # Full-text index backing keyword search and the sparse half of hybrid
        # retrieval. The two-argument to_tsvector form is IMMUTABLE, which is
        # what makes it indexable.
        Index(
            "ix_chunk_body_fts",
            text("to_tsvector('english'::regconfig, body)"),
            postgresql_using="gin",
        ),
        CheckConstraint("page_number >= 1", name="page_number_positive"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
    )


# =============================================================================
# Ingestion
# =============================================================================


class IngestionJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One run of the ingestion pipeline over a folder."""

    __tablename__ = "ingestion_job"

    status: Mapped[JobStatus] = mapped_column(
        _pg_enum(JobStatus, "job_status"),
        nullable=False,
        server_default=JobStatus.PENDING.value,
        index=True,
    )
    source_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # A full re-index re-embeds everything; otherwise unchanged chunks are kept.
    force_reindex: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    total_documents: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    processed_documents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    skipped_documents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    failed_documents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # One entry per failed document: filename, stage, error code, message.
    error_log: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("total_documents >= 0", name="total_documents_non_negative"),
        Index("ix_ingestion_job_created", "created_at"),
    )


# =============================================================================
# Chat
# =============================================================================


class ChatSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A conversation thread."""

    __tablename__ = "chat_session"

    title: Mapped[str | None] = mapped_column(String(300))
    # Sticky filters for the session, e.g. {"municipalities": ["coquitlam"]}.
    filters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChatMessage.created_at",
    )


class ChatMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single message, plus the full evidence behind an assistant answer."""

    __tablename__ = "chat_message"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ChatRole] = mapped_column(_pg_enum(ChatRole, "chat_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    intent: Mapped[QueryIntent | None] = mapped_column(_pg_enum(QueryIntent, "query_intent"))
    # Rendered citations: document, bylaw number, section, page, verbatim quote.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_band: Mapped[ConfidenceBand | None] = mapped_column(
        _pg_enum(ConfidenceBand, "confidence_band")
    )
    abstained: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # What was retrieved and how it scored. Non-negotiable for a legal tool:
    # when an answer is disputed months later, this reconstructs the reasoning.
    retrieval_trace: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    model_used: Mapped[str | None] = mapped_column(String(160))
    prompt_version: Mapped[str | None] = mapped_column(String(40))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)

    session: Mapped[ChatSession] = relationship(back_populates="messages")
    feedback: Mapped[list[AnswerFeedback]] = relationship(
        back_populates="message", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="confidence_range",
        ),
        Index("ix_chat_message_session_created", "session_id", "created_at"),
    )


class AnswerFeedback(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """User verdict on an answer.

    Feeds the admin review queue, and over time becomes the source of the golden
    evaluation set.
    """

    __tablename__ = "answer_feedback"

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_message.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rating: Mapped[FeedbackRating] = mapped_column(
        _pg_enum(FeedbackRating, "feedback_rating"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text)
    flagged_incorrect: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), index=True
    )
    reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    reviewed_by: Mapped[str | None] = mapped_column(String(160))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    message: Mapped[ChatMessage] = relationship(back_populates="feedback")
