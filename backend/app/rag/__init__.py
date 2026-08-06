"""Retrieval-augmented generation.

Phase 3 delivers the retrieval half:

    pgvector top 50  +  Postgres full-text top 50
        -> weighted RRF -> top 50
        -> [reranker seam] -> top 5
        -> LLM  (Phase 5)

Synthesis, citation verification and confidence scoring arrive in Phase 5. The
reranker interface is declared now so the pipeline shape does not change when
it lands.
"""

from app.rag.collections import CollectionSpec, model_slug
from app.rag.fusion import reciprocal_rank_fusion
from app.rag.results import RetrievalTrace, RetrievedChunk, SourceCoordinates

__all__ = [
    "CollectionSpec",
    "RetrievalTrace",
    "RetrievedChunk",
    "SourceCoordinates",
    "model_slug",
    "reciprocal_rank_fusion",
]
