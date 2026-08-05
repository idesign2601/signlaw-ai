"""Reranking providers.

Local cross-encoder only. Reranking is an accuracy optimisation, so a provider
that cannot load degrades to a no-op rather than failing the query.
"""

from __future__ import annotations

from pathlib import Path

from app.adapters.reranker.local import LocalCrossEncoderReranker
from app.core.config import EmbeddingSettings, RetrievalSettings

__all__ = ["LocalCrossEncoderReranker", "build_reranker"]


def build_reranker(
    retrieval: RetrievalSettings,
    embedding: EmbeddingSettings,
    *,
    cache_dir: str | None = None,
) -> LocalCrossEncoderReranker | None:
    """Construct the reranker, or ``None`` when disabled."""
    if not retrieval.rerank_enabled:
        return None

    return LocalCrossEncoderReranker(
        model=retrieval.rerank_model,
        device=embedding.device,
        batch_size=retrieval.rerank_batch_size,
        cache_dir=Path(cache_dir) if cache_dir else None,
    )
