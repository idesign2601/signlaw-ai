"""Hybrid retrieval over pgvector and Postgres full-text search.

Two retrievers, because bylaw queries need both. Dense embeddings handle
paraphrase — "can I put a sign on my storefront?" finds text about fascia signs
without sharing a word with it. Full-text handles the exact terms these
questions are full of: *fascia sign*, *sandwich board*, *Bylaw 4451*, *s. 5.3*,
where embeddings are semantically fuzzy and routinely miss the literal match.

Both run inside Postgres against the same rows, so filters — city, in-force
status, year — are applied *before* ranking rather than by retrieving broadly
and discarding afterwards. That is what makes "filter by city" both correct and
fast.

Shape of the pipeline::

    dense top 50  +  sparse top 50
        -> weighted RRF -> top 50
        -> reranker      -> top 5
        -> LLM
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.embeddings.base import EmbeddingProviderProtocol
from app.core.config import RetrievalSettings, VectorStoreSettings
from app.core.exceptions import IndexNotReadyError, RetrievalError
from app.core.logging import get_logger
from app.db.enums import ChunkType, DocumentStatus
from app.rag.fusion import reciprocal_rank_fusion
from app.rag.results import RetrievalTrace, RetrievedChunk, SourceCoordinates

__all__ = ["HybridRetriever", "RerankerProtocol", "RetrievalFilters"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """Pre-filters applied inside the SQL, before ranking."""

    municipality_slugs: tuple[str, ...] = ()
    # Defaults to in-force only. Repealed text must never surface by accident.
    in_force_only: bool = True
    document_ids: tuple[str, ...] = ()
    chunk_types: tuple[ChunkType, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    # Exclude OCR'd pages when a question demands precise numbers.
    exclude_ocr: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "municipalities": list(self.municipality_slugs),
            "in_force_only": self.in_force_only,
            "document_ids": list(self.document_ids),
            "chunk_types": [t.value for t in self.chunk_types],
            "year_from": self.year_from,
            "year_to": self.year_to,
            "exclude_ocr": self.exclude_ocr,
        }


class RerankerProtocol:
    """Reorders candidates by relevance to the query.

    Declared here as the seam Phase 4 fills. A cross-encoder scores the query
    and passage together rather than embedding them independently, which is
    markedly more accurate but too slow to run over the whole corpus — hence
    retrieve broadly, then rerank narrowly.
    """

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, top_n: int
    ) -> list[RetrievedChunk]:
        raise NotImplementedError


# Columns every retrieval returns. Shared between the two queries so a chunk
# carries identical provenance whichever retriever surfaced it.
_PROVENANCE_COLUMNS = """
    c.id                    AS chunk_id,
    c.body                  AS body,
    c.chunk_type            AS chunk_type,
    c.page_number           AS page_number,
    c.page_end              AS page_end,
    d.id                    AS document_id,
    d.title                 AS document_title,
    d.bylaw_number          AS bylaw_number,
    d.status                AS document_status,
    d.consolidation_date    AS consolidation_date,
    d.last_amendment_date   AS last_amendment_date,
    d.effective_date        AS effective_date,
    m.canonical_slug        AS municipality_slug,
    m.name                  AS municipality_name,
    s.section_number        AS section_number,
    s.full_path             AS section_path,
    s.heading               AS section_heading,
    p.was_ocred             AS from_ocr,
    p.extraction_confidence AS extraction_confidence,
    p.width                 AS page_width,
    p.height                AS page_height
"""

_JOINS = """
    FROM chunk c
    JOIN document d      ON d.id = c.document_id
    LEFT JOIN municipality m ON m.id = d.municipality_id
    LEFT JOIN section s      ON s.id = c.section_id
    LEFT JOIN page p         ON p.document_id = c.document_id
                            AND p.page_number = c.page_number
"""


@dataclass
class HybridRetriever:
    """Runs dense and sparse retrieval and fuses the results."""

    session: AsyncSession
    embedder: EmbeddingProviderProtocol
    settings: RetrievalSettings
    vector_settings: VectorStoreSettings
    reranker: RerankerProtocol | None = None
    _collection: tuple[str, str, int] | None = field(default=None, init=False, repr=False)

    # -- public API ----------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        *,
        filters: RetrievalFilters | None = None,
        top_n: int | None = None,
    ) -> tuple[list[RetrievedChunk], RetrievalTrace]:
        """Retrieve the best candidates for a query, with a full audit trace."""
        if not query.strip():
            raise RetrievalError("Cannot retrieve for an empty query.")

        started = time.perf_counter()
        filters = filters or RetrievalFilters(in_force_only=self.settings.in_force_only)
        limit = top_n or self.settings.rerank_top_n

        collection_id, collection_name, dimensions = await self._active_collection()

        dense = await self._dense_search(query, filters, collection_id, dimensions)
        sparse = await self._sparse_search(query, filters)

        fused = reciprocal_rank_fusion(
            dense,
            sparse,
            k=self.settings.rrf_k,
            dense_weight=self.settings.dense_weight,
            sparse_weight=self.settings.sparse_weight,
            limit=self.settings.candidate_pool_size,
        )

        reranked = False
        results = fused
        if self.reranker is not None and self.settings.rerank_enabled and fused:
            results = await self.reranker.rerank(query, fused, top_n=limit)
            reranked = True
        else:
            results = fused[:limit]

        duration_ms = int((time.perf_counter() - started) * 1000)
        trace = RetrievalTrace(
            query=query,
            collection=collection_name,
            filters=filters.as_dict(),
            dense_candidates=len(dense),
            sparse_candidates=len(sparse),
            fused_candidates=len(fused),
            returned=len(results),
            reranked=reranked,
            duration_ms=duration_ms,
            chunks=tuple(chunk.provenance() for chunk in results),
        )

        logger.info(
            "retrieval_completed",
            collection=collection_name,
            dense=len(dense),
            sparse=len(sparse),
            fused=len(fused),
            returned=len(results),
            reranked=reranked,
            duration_ms=duration_ms,
        )
        return results, trace

    # -- collection ----------------------------------------------------------

    async def _active_collection(self) -> tuple[str, str, int]:
        """Resolve the live collection.

        At most one collection is active — enforced by a partial unique index —
        so retrieval never has to choose between candidate indexes.
        """
        if self._collection is not None:
            return self._collection

        row = (
            await self.session.execute(
                text(
                    "SELECT id, name, dimensions FROM embedding_collection "
                    "WHERE status = 'active' LIMIT 1"
                )
            )
        ).first()

        if row is None:
            raise IndexNotReadyError(
                "No active embedding collection. Run an ingestion job and "
                "activate the resulting collection before asking questions."
            )

        self._collection = (str(row.id), str(row.name), int(row.dimensions))
        return self._collection

    # -- dense ---------------------------------------------------------------

    async def _dense_search(
        self,
        query: str,
        filters: RetrievalFilters,
        collection_id: str,
        dimensions: int,
    ) -> list[RetrievedChunk]:
        if self.settings.dense_top_k <= 0:
            return []

        vector = await self.embedder.embed_query(query)
        if len(vector) != dimensions:
            raise RetrievalError(
                f"Query embedding has {len(vector)} dimensions but the active "
                f"collection expects {dimensions}. The embedding model changed "
                f"without a re-index."
            )

        table = f"chunk_embedding_{dimensions}"
        operator = self.vector_settings.pgvector_operator
        where, params = self._build_filters(filters)

        # ef_search must exceed top-k or HNSW recall degrades sharply. SET LOCAL
        # scopes it to the current transaction rather than the pooled
        # connection, so it cannot leak into an unrelated query.
        ef_search = max(self.vector_settings.hnsw_ef_search, self.settings.dense_top_k * 2)
        await self.session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))

        sql = text(
            f"""
            SELECT {_PROVENANCE_COLUMNS},
                   1 - (e.embedding {operator} CAST(:query_vector AS vector)) AS score
            {_JOINS}
            JOIN {table} e ON e.chunk_id = c.id
            WHERE e.collection_id = CAST(:collection_id AS uuid)
              {where}
            ORDER BY e.embedding {operator} CAST(:query_vector AS vector)
            LIMIT :limit
            """
        )

        rows = await self.session.execute(
            sql,
            {
                **params,
                "collection_id": collection_id,
                "query_vector": _vector_literal(vector),
                "limit": self.settings.dense_top_k,
            },
        )
        return [_row_to_chunk(row, dense_score=float(row.score)) for row in rows]

    # -- sparse --------------------------------------------------------------

    async def _sparse_search(self, query: str, filters: RetrievalFilters) -> list[RetrievedChunk]:
        if self.settings.sparse_top_k <= 0:
            return []

        where, params = self._build_filters(filters)

        # websearch_to_tsquery accepts quoted phrases and OR, which maps well to
        # how people actually type these questions. ts_rank_cd weights term
        # proximity, so "maximum sign area" beats a chunk mentioning the three
        # words far apart.
        sql = text(
            f"""
            SELECT {_PROVENANCE_COLUMNS},
                   ts_rank_cd(
                       to_tsvector('english', c.body),
                       websearch_to_tsquery('english', :query)
                   ) AS score
            {_JOINS}
            WHERE to_tsvector('english', c.body)
                  @@ websearch_to_tsquery('english', :query)
              {where}
            ORDER BY score DESC, c.id
            LIMIT :limit
            """
        )

        try:
            rows = await self.session.execute(
                sql, {**params, "query": query, "limit": self.settings.sparse_top_k}
            )
        except Exception as exc:
            logger.warning("sparse_search_failed", error=str(exc))
            return []

        return [_row_to_chunk(row, sparse_score=float(row.score)) for row in rows]

    # -- filters -------------------------------------------------------------

    @staticmethod
    def _build_filters(filters: RetrievalFilters) -> tuple[str, dict[str, object]]:
        """Build the shared WHERE fragment.

        Applied inside both queries so filtering happens before ranking. The
        in-force clause is the important one: without it, retrieval will
        cheerfully return repealed text that reads exactly like current law.
        """
        clauses: list[str] = []
        params: dict[str, object] = {}

        if filters.municipality_slugs:
            clauses.append("m.canonical_slug = ANY(:municipality_slugs)")
            params["municipality_slugs"] = list(filters.municipality_slugs)

        if filters.in_force_only:
            clauses.append("d.status = 'in_force'")

        if filters.document_ids:
            clauses.append("d.id = ANY(CAST(:document_ids AS uuid[]))")
            params["document_ids"] = list(filters.document_ids)

        if filters.chunk_types:
            clauses.append("c.chunk_type = ANY(:chunk_types)")
            params["chunk_types"] = [t.value for t in filters.chunk_types]

        if filters.year_from is not None:
            clauses.append("d.year >= :year_from")
            params["year_from"] = filters.year_from

        if filters.year_to is not None:
            clauses.append("d.year <= :year_to")
            params["year_to"] = filters.year_to

        if filters.exclude_ocr:
            clauses.append("COALESCE(p.was_ocred, false) = false")

        where = "".join(f"  AND {clause}\n" for clause in clauses)
        return (f"\n{where}" if where else ""), params


def _vector_literal(vector: Sequence[float]) -> str:
    """Render a vector in pgvector's text input format."""
    return "[" + ",".join(f"{value:.7g}" for value in vector) + "]"


def _row_to_chunk(
    row: object, *, dense_score: float | None = None, sparse_score: float | None = None
) -> RetrievedChunk:
    """Map a result row onto the citation-carrying result object."""
    coordinates = None
    width = getattr(row, "page_width", None)
    height = getattr(row, "page_height", None)
    if width and height:
        # Chunk-level bboxes arrive in Phase 4 with per-line coordinates; page
        # geometry is recorded now so the viewer can already scale a highlight.
        coordinates = SourceCoordinates(
            x0=0.0, y0=0.0, x1=0.0, y1=0.0, page_width=float(width), page_height=float(height)
        )

    raw_status = getattr(row, "document_status", None)
    status = DocumentStatus(raw_status) if raw_status else DocumentStatus.UNKNOWN
    raw_type = getattr(row, "chunk_type", None)
    chunk_type = ChunkType(raw_type) if raw_type else ChunkType.PROSE

    return RetrievedChunk(
        chunk_id=str(row.chunk_id),
        body=row.body,
        chunk_type=chunk_type,
        document_id=str(row.document_id),
        document_title=row.document_title,
        municipality_slug=row.municipality_slug,
        municipality_name=row.municipality_name,
        bylaw_number=row.bylaw_number,
        section_number=row.section_number,
        section_path=row.section_path,
        section_heading=row.section_heading,
        page_number=int(row.page_number),
        page_end=int(row.page_end) if row.page_end else None,
        document_status=status,
        consolidation_date=_as_date(getattr(row, "consolidation_date", None)),
        last_amendment_date=_as_date(getattr(row, "last_amendment_date", None)),
        effective_date=_as_date(getattr(row, "effective_date", None)),
        from_ocr=bool(getattr(row, "from_ocr", False)),
        extraction_confidence=float(getattr(row, "extraction_confidence", 1.0) or 1.0),
        coordinates=coordinates,
        dense_score=dense_score,
        sparse_score=sparse_score,
    )


def _as_date(value: object) -> date | None:
    return value if isinstance(value, date) else None
