"""Enumerations persisted in the database.

These are stored as native Postgres enum types. Adding a member requires a
migration; removing one is a breaking change. Values are lower-case snake_case
and form part of the API contract.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ChatRole",
    "ChunkType",
    "CollectionStatus",
    "ConfidenceBand",
    "DocType",
    "DocumentStatus",
    "ExtractionMethod",
    "FeedbackRating",
    "JobStatus",
    "MetadataSource",
    "ProcessingStage",
    "QueryIntent",
    "RelationType",
]


class DocType(StrEnum):
    """What kind of document this is within a bylaw lineage."""

    BASE = "base"
    """The original enacting bylaw."""

    AMENDMENT = "amendment"
    """A bylaw that amends another bylaw."""

    CONSOLIDATED = "consolidated"
    """A consolidated-for-convenience version incorporating amendments."""

    SCHEDULE = "schedule"
    """A schedule or appendix, often the tables of permitted sign dimensions."""

    POLICY = "policy"
    """Guidance or policy material that is not itself enacted law."""

    UNKNOWN = "unknown"


class DocumentStatus(StrEnum):
    """Whether the document's text is currently the law.

    Retrieval defaults to ``IN_FORCE`` only. Citing superseded text is the
    primary correctness risk in this system.
    """

    IN_FORCE = "in_force"
    SUPERSEDED = "superseded"
    REPEALED = "repealed"
    UNKNOWN = "unknown"


class RelationType(StrEnum):
    """How one document relates to another."""

    AMENDS = "amends"
    CONSOLIDATES = "consolidates"
    REPEALS = "repeals"
    REPLACES = "replaces"


class MetadataSource(StrEnum):
    """How a document's metadata was determined, in ascending order of trust."""

    FILENAME = "filename"
    REGEX = "regex"
    LLM = "llm"
    HUMAN = "human"


class ChunkType(StrEnum):
    """What the chunk contains.

    Tables are tracked separately because they carry the numeric limits that
    most questions ultimately turn on, and because they must never be split.
    """

    PROSE = "prose"
    TABLE = "table"
    DEFINITION = "definition"
    SCHEDULE = "schedule"
    HEADING = "heading"


class JobStatus(StrEnum):
    """Lifecycle of an ingestion job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.COMPLETED,
            JobStatus.COMPLETED_WITH_ERRORS,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


class CollectionStatus(StrEnum):
    """Lifecycle of a versioned embedding collection.

    Rebuilding is additive: a new collection is populated to ``BUILDING`` while
    the ``ACTIVE`` one keeps serving queries, then the two swap in a single
    transaction. There is never a window where the index is half-rebuilt, and
    rollback is one UPDATE.
    """

    BUILDING = "building"
    """Being populated. Never queried."""

    ACTIVE = "active"
    """Serving retrieval. At most one collection is active at a time."""

    RETIRED = "retired"
    """Superseded but retained, so a bad rebuild can be rolled back instantly."""

    FAILED = "failed"
    """Abandoned mid-build."""


class ProcessingStage(StrEnum):
    """How far a document has progressed through ingestion.

    Recorded per document so a run over 500 PDFs that dies on document 120
    resumes from document 120's last completed stage rather than from zero. The
    declared order is what :meth:`is_at_least` compares against, so members must
    stay in pipeline order.
    """

    UPLOADED = "uploaded"
    """Discovered, hashed and registered. No content read yet."""

    EXTRACTED = "extracted"
    """Text and layout recovered by PyMuPDF."""

    OCR_COMPLETED = "ocr_completed"
    """OCR fallback finished. Reached only by documents that needed it."""

    TABLES_EXTRACTED = "tables_extracted"
    """Tables lifted out with their structure preserved."""

    METADATA_DETECTED = "metadata_detected"
    """Municipality, title, bylaw number, version and amendment dates resolved."""

    SECTIONS_PARSED = "sections_parsed"
    """Section hierarchy built — the citation backbone."""

    CHUNKED = "chunked"
    """Text split into citable chunks."""

    EMBEDDED = "embedded"
    """Vectors generated for every chunk."""

    INDEXED = "indexed"
    """Upserted into the live vector collection. Retrievable."""

    FAILED = "failed"
    """Halted. ``document.ingestion_error`` says where and why."""

    @property
    def order(self) -> int:
        """Position in the pipeline. ``FAILED`` sorts before everything."""
        return _STAGE_ORDER[self]

    @property
    def is_terminal(self) -> bool:
        return self in {ProcessingStage.INDEXED, ProcessingStage.FAILED}

    def is_at_least(self, other: ProcessingStage) -> bool:
        """Whether this stage has reached or passed ``other``.

        A failed document has reached nothing, so resuming always re-runs it
        from the beginning rather than trusting partial output.
        """
        if self is ProcessingStage.FAILED:
            return False
        return self.order >= other.order


_STAGE_ORDER: dict[ProcessingStage, int] = {
    ProcessingStage.FAILED: -1,
    ProcessingStage.UPLOADED: 0,
    ProcessingStage.EXTRACTED: 1,
    ProcessingStage.OCR_COMPLETED: 2,
    ProcessingStage.TABLES_EXTRACTED: 3,
    ProcessingStage.METADATA_DETECTED: 4,
    ProcessingStage.SECTIONS_PARSED: 5,
    ProcessingStage.CHUNKED: 6,
    ProcessingStage.EMBEDDED: 7,
    ProcessingStage.INDEXED: 8,
}


class ExtractionMethod(StrEnum):
    """How a page's text was obtained.

    Recorded per page because it drives the confidence penalty: OCR output has
    no reliable section numbering or table geometry, and must never be treated
    as equal in quality to a clean text layer.
    """

    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    MIXED = "mixed"
    FAILED = "failed"


class ChatRole(StrEnum):
    """Author of a chat message."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class QueryIntent(StrEnum):
    """Classification produced by the query router."""

    SINGLE_CITY = "single_city"
    MULTI_CITY_COMPARE = "multi_city_compare"
    KEYWORD = "keyword"
    DEFINITION = "definition"
    OUT_OF_SCOPE = "out_of_scope"


class ConfidenceBand(StrEnum):
    """User-facing confidence bucket.

    A bare number means little to a reader; a calibrated band with an
    explanation does.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


class FeedbackRating(StrEnum):
    """User verdict on an answer."""

    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"
    INCORRECT = "incorrect"
