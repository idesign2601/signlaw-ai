"""Rank fusion for hybrid retrieval.

Two retrievers with incomparable score scales: pgvector returns cosine
distances, Postgres full-text returns ``ts_rank_cd`` values whose magnitude
depends on document length and term frequency. Normalising them onto a shared
scale is fragile — one outlier drags the whole normalisation — so fusion works
on **ranks**, which are directly comparable.

Weighted Reciprocal Rank Fusion::

    score(d) = Σ  weight_r / (k + rank_r(d))

``k`` (default 60) damps the advantage of the very top positions, so a document
ranked 1 by one retriever and 40 by the other does not automatically beat one
ranked 3 and 4 by both. That matters here: a chunk both retrievers agree on is
usually a better citation than one only the embedding model liked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from app.rag.results import RetrievedChunk

__all__ = ["reciprocal_rank_fusion"]


def reciprocal_rank_fusion(
    dense: Sequence[RetrievedChunk],
    sparse: Sequence[RetrievedChunk],
    *,
    k: int = 60,
    dense_weight: float = 0.5,
    sparse_weight: float = 0.5,
    limit: int | None = None,
) -> list[RetrievedChunk]:
    """Fuse two ranked lists into one.

    Inputs must already be ordered best-first. Chunks appearing in both lists
    keep the scores and ranks from each, so the trace records why a result
    surfaced.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if dense_weight + sparse_weight <= 0:
        raise ValueError("at least one weight must be positive")

    merged: dict[str, RetrievedChunk] = {}
    scores: dict[str, float] = {}

    for rank, chunk in enumerate(dense, start=1):
        merged[chunk.chunk_id] = replace(chunk, dense_rank=rank)
        scores[chunk.chunk_id] = dense_weight / (k + rank)

    for rank, chunk in enumerate(sparse, start=1):
        contribution = sparse_weight / (k + rank)
        existing = merged.get(chunk.chunk_id)

        if existing is None:
            merged[chunk.chunk_id] = replace(chunk, sparse_rank=rank)
            scores[chunk.chunk_id] = contribution
            continue

        # Seen by both retrievers: keep every signal for the audit trail.
        merged[chunk.chunk_id] = replace(
            existing,
            sparse_rank=rank,
            sparse_score=chunk.sparse_score,
        )
        scores[chunk.chunk_id] += contribution

    fused = [replace(chunk, fused_score=scores[chunk_id]) for chunk_id, chunk in merged.items()]
    fused.sort(key=lambda chunk: (-chunk.fused_score, chunk.chunk_id))

    return fused[:limit] if limit is not None else fused
