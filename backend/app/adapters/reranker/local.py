"""Local cross-encoder reranking.

Fills the seam declared in Phase 3. The retrievers embed the query and each
passage *independently*, so relevance is judged by distance in a shared space
— fast enough to scan the whole corpus, but blind to interaction between the
question and the text. A cross-encoder reads query and passage **together** and
scores the pair directly. It is far more accurate and far too slow to run over
everything, which is exactly why the pipeline retrieves 50 and reranks to 5.

This matters more here than in general RAG. Sign bylaws repeat near-identical
language across sign types and zones, so the top 50 routinely contains several
passages that look equally relevant to a bi-encoder and only one that actually
answers the question. Picking the wrong one produces a confident citation to the
wrong clause.

Runs locally on the mounted models volume. Nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.rag.results import RetrievedChunk

__all__ = ["LocalCrossEncoderReranker"]

logger = get_logger(__name__)

# Passages are truncated before scoring: cross-encoders have a fixed input
# budget shared between query and passage, and a long table would otherwise
# push the query itself out of the window.
_MAX_PASSAGE_CHARS = 4000


class LocalCrossEncoderReranker:
    """Reorders candidates with a sentence-transformers CrossEncoder.

    Degrades to a no-op if the model cannot be loaded. That is deliberate: a
    missing reranker should cost accuracy, not availability — the fused ordering
    is still a reasonable answer, and failing the whole query would be worse.
    """

    def __init__(
        self,
        *,
        model: str = "BAAI/bge-reranker-v2-m3",
        device: str = "auto",
        batch_size: int = 16,
        cache_dir: Path | None = None,
        max_length: int = 512,
    ) -> None:
        self._model_name = model
        self._device = device
        self._batch_size = batch_size
        self._cache_dir = cache_dir
        self._max_length = max_length
        self._model: Any | None = None
        self._unavailable_reason: str | None = None
        self._load_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model_name

    # -- public API ----------------------------------------------------------

    async def rerank(
        self, query: str, chunks: Sequence[RetrievedChunk], *, top_n: int
    ) -> list[RetrievedChunk]:
        """Score every candidate against the query and return the best ``top_n``."""
        if not chunks:
            return []
        if top_n >= len(chunks) and len(chunks) == 1:
            return list(chunks)

        model = await self._ensure_loaded()
        if model is None:
            # Fused order is a reasonable fallback; the trace records that no
            # reranking happened so a low-quality answer is explainable.
            logger.warning(
                "rerank_skipped",
                reason=self._unavailable_reason,
                candidates=len(chunks),
            )
            return list(chunks[:top_n])

        pairs = [(query, self._passage_for(chunk)) for chunk in chunks]

        try:
            scores = await asyncio.to_thread(self._score, model, pairs)
        except Exception as exc:  # never fail a query on reranking
            logger.warning("rerank_failed", error=str(exc), candidates=len(chunks))
            return list(chunks[:top_n])

        scored = [
            chunk.with_rerank_score(float(score))
            for chunk, score in zip(chunks, scores, strict=True)
        ]
        # chunk_id breaks ties deterministically: two runs of the same question
        # must produce the same citations.
        scored.sort(key=lambda chunk: (-(chunk.rerank_score or 0.0), chunk.chunk_id))

        logger.info(
            "rerank_completed",
            model=self._model_name,
            candidates=len(chunks),
            returned=min(top_n, len(scored)),
            top_score=round(scored[0].rerank_score or 0.0, 4),
        )
        return scored[:top_n]

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _passage_for(chunk: RetrievedChunk) -> str:
        """Text handed to the cross-encoder.

        The section heading is prepended because it is often the only place the
        sign type appears — a clause reading "must not exceed 20% of the
        building face" is far more obviously about fascia signs when scored
        under its own heading.
        """
        parts: list[str] = []
        if chunk.section_number or chunk.section_heading:
            label = " ".join(part for part in (chunk.section_number, chunk.section_heading) if part)
            parts.append(label)
        parts.append(chunk.body[:_MAX_PASSAGE_CHARS])
        return "\n".join(parts)

    def _score(self, model: Any, pairs: Sequence[tuple[str, str]]) -> list[float]:
        raw = model.predict(
            list(pairs),
            batch_size=self._batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(value) for value in raw]

    async def _ensure_loaded(self) -> Any | None:
        """Load the model once, tolerating concurrent callers.

        Both pieces of state are read through locals rather than tested as
        attributes. Testing ``self._x is not None`` narrows the *attribute*, and
        mypy keeps that narrowing across the lock acquisition — so the re-check
        inside the lock would be typed as dead code even though at runtime
        another coroutine may have changed it. Assigning to a local instead
        re-widens on each read, which is both what mypy needs and what the
        double-checked lock actually means.
        """
        cached = self._model
        reason = self._unavailable_reason
        if cached is not None:
            return cached
        if reason is not None:
            return None

        async with self._load_lock:
            # Re-read: another coroutine may have loaded, or failed to load,
            # while this one waited for the lock.
            cached = self._model
            reason = self._unavailable_reason
            if cached is not None:
                return cached
            if reason is not None:
                return None
            self._model = await asyncio.to_thread(self._load)
            return self._model

    def _load(self) -> Any | None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            self._unavailable_reason = (
                "sentence-transformers is not installed; install the 'local' extra"
            )
            return None

        try:
            logger.info("reranker_loading", model=self._model_name)
            model = CrossEncoder(
                self._model_name,
                device=self._resolve_device(),
                max_length=self._max_length,
                cache_folder=str(self._cache_dir) if self._cache_dir else None,
            )
        except Exception as exc:  # degrade, do not fail
            self._unavailable_reason = (
                f"could not load '{self._model_name}': {exc}. Run `make fetch-models`."
            )
            logger.warning("reranker_unavailable", reason=self._unavailable_reason)
            return None

        logger.info("reranker_loaded", model=self._model_name)
        return model

    def _resolve_device(self) -> str:
        if self._device != "auto":
            return self._device
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if torch.backends.mps.is_available():
                return "mps"
        except Exception:  # noqa: S110 — falling back to CPU is the whole point
            pass
        return "cpu"

    async def health(self) -> tuple[bool, str]:
        model = await self._ensure_loaded()
        if model is None:
            return False, self._unavailable_reason or "reranker unavailable"
        return True, f"{self._model_name} ready"
