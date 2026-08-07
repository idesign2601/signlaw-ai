"""Local embeddings via sentence-transformers. Default: BGE-M3.

Nothing leaves the machine. Weights are read from the mounted models volume
(``HF_HOME``), never downloaded at request time and never baked into an image,
so an air-gapped install works by copying ``data/models/`` across.

BGE-M3 is the default because it handles the two things this corpus demands:
long context (bylaw sections run past 512 tokens, where most encoders truncate)
and strong retrieval quality on domain-specific terminology without fine-tuning.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path
from typing import Any

from app.adapters.embeddings.base import EmbeddingResult
from app.core.exceptions import EmbeddingError
from app.core.logging import get_logger

__all__ = ["LocalEmbeddingProvider"]

logger = get_logger(__name__)

# BGE and E5 family models are trained with an asymmetric objective: queries get
# an instruction prefix, passages do not. Omitting it costs several points of
# recall, so the mapping is applied automatically per model family.
_QUERY_PREFIXES: dict[str, str] = {
    "bge-large-en": "Represent this sentence for searching relevant passages: ",
    "bge-base-en": "Represent this sentence for searching relevant passages: ",
    "bge-small-en": "Represent this sentence for searching relevant passages: ",
    "e5-large": "query: ",
    "e5-base": "query: ",
    "e5-small": "query: ",
    "multilingual-e5": "query: ",
}

_PASSAGE_PREFIXES: dict[str, str] = {
    "e5-large": "passage: ",
    "e5-base": "passage: ",
    "e5-small": "passage: ",
    "multilingual-e5": "passage: ",
}


def _reported_dimensions(model: Any) -> int:
    """Output width a loaded model claims, across sentence-transformers versions.

    ``get_sentence_embedding_dimension`` was renamed to ``get_embedding_dimension``
    and now emits a ``FutureWarning``. The suite runs with
    ``filterwarnings = ["error"]``, so calling the old name unconditionally turns
    into a hard failure the moment a test loads a real model.

    Returns 0 when neither accessor exists, which the caller treats as "unknown"
    and skips the dimension check rather than rejecting a working model.
    """
    for accessor in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        method = getattr(model, accessor, None)
        if callable(method):
            return int(method() or 0)
    return 0


class LocalEmbeddingProvider:
    """sentence-transformers embeddings, run in-process.

    The model is loaded lazily on first use rather than at import, so the API
    process starts fast and a worker that never embeds never pays the cost of
    loading several gigabytes of weights.

    Encoding is CPU- or GPU-bound and releases the GIL inside torch, so calls
    are dispatched to a thread to keep the event loop responsive.
    """

    def __init__(
        self,
        *,
        model: str = "BAAI/bge-m3",
        dimensions: int = 1024,
        device: str = "auto",
        batch_size: int = 32,
        normalize: bool = True,
        cache_dir: Path | None = None,
        trust_remote_code: bool = False,
    ) -> None:
        self._model_name = model
        self._dimensions = dimensions
        self._device = device
        self._batch_size = batch_size
        self._normalize = normalize
        self._cache_dir = cache_dir
        self._trust_remote_code = trust_remote_code
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    # -- identity ------------------------------------------------------------

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @cached_property
    def model_revision(self) -> str | None:
        """Short hash of the model identity and configuration.

        A real weights hash would require reading every safetensors shard on
        startup. This captures the configuration that determines the vector
        space, which is what makes two collections comparable or not.
        """
        material = f"{self._model_name}|{self._dimensions}|{self._normalize}"
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    @cached_property
    def _query_prefix(self) -> str:
        return self._prefix_from(_QUERY_PREFIXES)

    @cached_property
    def _passage_prefix(self) -> str:
        return self._prefix_from(_PASSAGE_PREFIXES)

    def _prefix_from(self, table: dict[str, str]) -> str:
        lowered = self._model_name.lower()
        # BGE-M3 is trained without an instruction prefix, unlike bge-*-en.
        if "bge-m3" in lowered:
            return ""
        for fragment, prefix in table.items():
            if fragment in lowered:
                return prefix
        return ""

    # -- loading -------------------------------------------------------------

    async def _ensure_loaded(self) -> Any:
        # Read through a local so mypy does not narrow `self._model` across the
        # await and conclude the re-check below is unreachable.
        cached = self._model
        if cached is not None:
            return cached

        async with self._load_lock:
            # Re-check: another coroutine may have loaded while we waited.
            cached = self._model
            if cached is not None:
                return cached
            self._model = await asyncio.to_thread(self._load)
            return self._model

    def _load(self) -> Any:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingError(
                "sentence-transformers is not installed. Install the backend "
                "with the 'local' extra: pip install -e '.[local]'",
                cause=exc,
            ) from exc

        device = self._resolve_device()
        logger.info(
            "embedding_model_loading",
            model=self._model_name,
            device=device,
            cache_dir=str(self._cache_dir) if self._cache_dir else None,
        )

        try:
            model = SentenceTransformer(
                self._model_name,
                device=device,
                cache_folder=str(self._cache_dir) if self._cache_dir else None,
                trust_remote_code=self._trust_remote_code,
            )
        except Exception as exc:
            raise EmbeddingError(
                f"Could not load embedding model '{self._model_name}': {exc}. "
                "Run `make fetch-models` to download it into the models volume.",
                cause=exc,
            ) from exc

        actual = _reported_dimensions(model)
        if actual and actual != self._dimensions:
            # Catching this here prevents writing vectors of the wrong width
            # into a pgvector column, which would fail per-row much later.
            raise EmbeddingError(
                f"Model '{self._model_name}' produces {actual}-dimensional vectors "
                f"but EMBEDDING__DIMENSIONS is {self._dimensions}."
            )

        logger.info("embedding_model_loaded", model=self._model_name, dimensions=actual)
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

    # -- embedding -----------------------------------------------------------

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed corpus text for storage."""
        if not texts:
            return EmbeddingResult((), self._model_name, self._dimensions, self.model_revision)

        model = await self._ensure_loaded()
        prefixed = [f"{self._passage_prefix}{text}" for text in texts]

        try:
            vectors = await asyncio.to_thread(self._encode, model, prefixed)
        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(
                f"Embedding failed for {len(texts)} texts: {exc}", cause=exc
            ) from exc

        return EmbeddingResult(
            vectors=vectors,
            model=self._model_name,
            dimensions=self._dimensions,
            model_revision=self.model_revision,
        )

    async def embed_query(self, text: str) -> tuple[float, ...]:
        """Embed a user question for search."""
        if not text.strip():
            raise EmbeddingError("Cannot embed an empty query.")

        model = await self._ensure_loaded()
        prefixed = f"{self._query_prefix}{text}"

        try:
            vectors = await asyncio.to_thread(self._encode, model, [prefixed])
        except Exception as exc:
            raise EmbeddingError(f"Query embedding failed: {exc}", cause=exc) from exc

        return vectors[0]

    def _encode(self, model: Any, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        raw = model.encode(
            list(texts),
            batch_size=self._batch_size,
            # Normalised vectors make cosine distance equivalent to a dot
            # product, which is what pgvector's cosine operator assumes.
            normalize_embeddings=self._normalize,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return tuple(tuple(float(value) for value in row) for row in raw)

    # -- health --------------------------------------------------------------

    async def health(self) -> tuple[bool, str]:
        """Whether the model can be loaded and produce a vector."""
        try:
            vector = await self.embed_query("health check")
        except EmbeddingError as exc:
            return False, exc.message
        except Exception as exc:  # health checks never propagate
            return False, str(exc)

        if len(vector) != self._dimensions:
            return False, (
                f"model returned {len(vector)} dimensions, expected {self._dimensions}"
            )
        return True, f"{self._model_name} ready ({self._dimensions}d)"
