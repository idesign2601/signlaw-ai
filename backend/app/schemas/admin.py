"""Admin response contracts.

The dashboard needs one coarse status per document. The database already tracks
a ten-stage :class:`~app.db.enums.ProcessingStage` for resumability, which is
the right granularity for an operator debugging a stuck ingest and the wrong
granularity for a table of documents. :class:`DocumentState` collapses it.

Deriving rather than storing a second status column matters: two columns
recording the same fact drift, and the one the dashboard reads would eventually
disagree with the one ingestion writes.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.db.enums import JobStatus, ProcessingStage

__all__ = [
    "DocumentListResponse",
    "DocumentState",
    "DocumentSummary",
    "PendingUpload",
    "UploadAccepted",
    "ZoningConfigRequest",
]


class DocumentState(StrEnum):
    """Coarse status for the dashboard."""

    UPLOADED = "uploaded"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def from_stage(cls, stage: str | None) -> DocumentState:
        """Collapse a pipeline stage into a dashboard status."""
        if stage == ProcessingStage.FAILED.value:
            return cls.FAILED
        if stage == ProcessingStage.INDEXED.value:
            return cls.COMPLETED
        if stage == ProcessingStage.UPLOADED.value or stage is None:
            return cls.UPLOADED
        # Extracted, chunked, embedded and the rest are all "still working".
        return cls.PROCESSING

    @classmethod
    def from_job(cls, status: str | None) -> DocumentState:
        """Collapse an ingestion job status, for uploads with no document yet."""
        if status == JobStatus.PENDING.value:
            return cls.UPLOADED
        if status == JobStatus.RUNNING.value:
            return cls.PROCESSING
        if status in {
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
            JobStatus.COMPLETED_WITH_ERRORS.value,
        }:
            return cls.FAILED
        return cls.COMPLETED


class UploadAccepted(BaseModel):
    """Returned immediately; ingestion continues in the background.

    Extraction, OCR, chunking and embedding take minutes on a large scanned
    bylaw. Holding the HTTP connection open for that would time out at every
    proxy between here and the browser, so the upload is acknowledged and the
    dashboard is where progress is watched.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    filename: str
    state: DocumentState = DocumentState.UPLOADED
    message: str


class DocumentSummary(BaseModel):
    """One indexed document, as the dashboard lists it."""

    model_config = ConfigDict(frozen=True)

    id: str
    municipality: str | None
    filename: str
    title: str | None
    bylaw_number: str | None
    year: int | None
    pages: int
    chunks: int
    state: DocumentState
    status: str = Field(description="in_force, superseded, repealed or unknown.")
    uploaded_at: datetime
    indexed_at: datetime | None = None
    failed_stage: str | None = Field(
        default=None, description="Pipeline stage that failed, when state is failed."
    )
    ocr_applied: bool = False
    text_quality: float | None = Field(
        default=None,
        description=(
            "Mean extraction confidence, 0–1. Materially below 1 means pages "
            "were recovered by OCR or extracted poorly, and citations from them "
            "deserve checking."
        ),
    )


class PendingUpload(BaseModel):
    """An upload still being processed, before it exists as a document.

    Listed separately so an operator can tell "still working" from "finished and
    produced nothing", which otherwise look identical in an empty table.
    """

    model_config = ConfigDict(frozen=True)

    job_id: str
    filename: str
    state: DocumentState
    created_at: datetime
    error: str | None = None


class DocumentListResponse(BaseModel):
    """Everything the dashboard renders."""

    model_config = ConfigDict(frozen=True)

    documents: list[DocumentSummary]
    pending: list[PendingUpload]
    total: int


class ZoningConfigRequest(BaseModel):
    """A municipality's zoning service configuration.

    ``kind`` names a query grammar — arcgis, opendatasoft, socrata — and
    ``config`` carries everything specific to the city: dataset identifiers and
    which attribute holds the zone. Adding a municipality is this payload plus
    its bylaw PDFs; no Python changes.
    """

    model_config = ConfigDict(frozen=True)

    kind: str | None = Field(
        default=None, description="arcgis, opendatasoft, socrata, or null to disable."
    )
    endpoint: str | None = Field(default=None, max_length=1000)
    config: dict[str, object] = Field(
        default_factory=dict,
        description=(
            'Field mapping and dataset identifiers, e.g. {"dataset": "zoning", '
            '"fields": {"zoning_code": "ZONE", "address": "CIVIC_ADDRESS"}}.'
        ),
    )
    map_url: str | None = Field(default=None, max_length=1000)
    verified: bool = Field(
        default=False,
        description=(
            "Whether this has been checked against the city's own service "
            "directory. **The provider is not queried until this is true.** An "
            "endpoint that responds but carries a similar-looking field returns "
            "a confidently wrong zone, which is worse than returning nothing."
        ),
    )
