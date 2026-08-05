"""Retrieval results.

Every retrieved chunk carries a complete, verifiable provenance record. This is
not decoration: the answer synthesiser cannot cite what retrieval did not
return, and the verification pass checks quotes against exactly these objects.

A chunk that reaches the model without a municipality, section and page is a
chunk that cannot be cited — so the shape below makes that state visible
(:attr:`RetrievedChunk.is_citable`) rather than letting it pass silently.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

from app.db.enums import ChunkType, DocumentStatus

__all__ = ["RetrievalTrace", "RetrievedChunk", "SourceCoordinates"]


@dataclass(frozen=True, slots=True)
class SourceCoordinates:
    """Where the text sits on the rendered page, in PDF points.

    Lets the viewer highlight the quoted passage rather than only opening the
    page. Page geometry travels alongside because a bbox is meaningless without
    the page size it was measured against.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float | None = None
    page_height: float | None = None

    def as_ratios(self) -> tuple[float, float, float, float] | None:
        """Position as fractions of the page, for rendering at any zoom."""
        if not self.page_width or not self.page_height:
            return None
        return (
            self.x0 / self.page_width,
            self.y0 / self.page_height,
            self.x1 / self.page_width,
            self.y1 / self.page_height,
        )


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One candidate, with everything needed to cite and to judge it."""

    chunk_id: str
    body: str
    chunk_type: ChunkType

    # --- citation ---------------------------------------------------------
    document_id: str
    document_title: str | None
    municipality_slug: str | None
    municipality_name: str | None
    bylaw_number: str | None
    section_number: str | None
    section_path: str | None
    section_heading: str | None
    page_number: int
    page_end: int | None = None

    # --- currency ---------------------------------------------------------
    # Whether this text is still the law. Retrieval defaults to in-force only,
    # but the status travels with the chunk so an answer can say so explicitly.
    document_status: DocumentStatus = DocumentStatus.UNKNOWN
    consolidation_date: date | None = None
    last_amendment_date: date | None = None
    effective_date: date | None = None

    # --- provenance quality ----------------------------------------------
    from_ocr: bool = False
    extraction_confidence: float = 1.0
    coordinates: SourceCoordinates | None = None

    # --- scoring ----------------------------------------------------------
    dense_score: float | None = None
    dense_rank: int | None = None
    sparse_score: float | None = None
    sparse_rank: int | None = None
    fused_score: float = 0.0
    rerank_score: float | None = None

    @property
    def is_citable(self) -> bool:
        """Whether this chunk can support a clause-level citation.

        Front matter and text preceding the first recognised heading cannot.
        Such chunks are still usable as context but must not be presented as
        though they were an identifiable provision.
        """
        return bool(
            self.municipality_slug and self.document_title and self.section_number
        )

    @property
    def is_current(self) -> bool:
        return self.document_status is DocumentStatus.IN_FORCE

    @property
    def final_score(self) -> float:
        """Score used for ordering — the reranker's verdict when it ran."""
        return self.rerank_score if self.rerank_score is not None else self.fused_score

    @property
    def citation_label(self) -> str:
        """e.g. ``Coquitlam — Sign Bylaw No. 4451, s. 5.3(b), p. 22``."""
        parts: list[str] = []
        if self.municipality_name:
            parts.append(self.municipality_name)
        if self.document_title:
            parts.append(self.document_title)
        head = " — ".join(parts) if parts else "Unattributed document"

        tail = [f"s. {self.section_number}"] if self.section_number else []
        tail.append(f"p. {self.page_number}")
        return f"{head}, {', '.join(tail)}"

    def with_rerank_score(self, score: float) -> RetrievedChunk:
        return replace(self, rerank_score=score)

    def provenance(self) -> dict[str, object]:
        """Flat record for the persisted retrieval trace."""
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "municipality": self.municipality_slug,
            "bylaw_number": self.bylaw_number,
            "section": self.section_number,
            "section_path": self.section_path,
            "page": self.page_number,
            "status": self.document_status.value,
            "consolidation_date": (
                self.consolidation_date.isoformat() if self.consolidation_date else None
            ),
            "last_amendment_date": (
                self.last_amendment_date.isoformat()
                if self.last_amendment_date
                else None
            ),
            "from_ocr": self.from_ocr,
            "dense_rank": self.dense_rank,
            "sparse_rank": self.sparse_rank,
            "fused_score": round(self.fused_score, 6),
            "rerank_score": (
                round(self.rerank_score, 6) if self.rerank_score is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """What retrieval did, persisted with every answer.

    Non-negotiable for a legal tool: when an answer is disputed months later,
    this reconstructs which chunks were considered, how they scored and which
    filters were applied.
    """

    query: str
    collection: str
    filters: dict[str, object]
    dense_candidates: int
    sparse_candidates: int
    fused_candidates: int
    returned: int
    reranked: bool
    duration_ms: int
    chunks: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "collection": self.collection,
            "filters": self.filters,
            "counts": {
                "dense": self.dense_candidates,
                "sparse": self.sparse_candidates,
                "fused": self.fused_candidates,
                "returned": self.returned,
            },
            "reranked": self.reranked,
            "duration_ms": self.duration_ms,
            "chunks": list(self.chunks),
        }
