"""Embedding provider interface.

A Protocol rather than a base class, so an implementation only has to satisfy
the shape. Swapping providers is a configuration change; nothing above this
layer knows which model produced a vector.

Queries and documents are embedded through **separate methods** on purpose.
Instruction-tuned retrieval models — BGE, E5, GTE — expect an asymmetric prompt
prefix, and embedding a query as though it were a document measurably degrades
recall. Keeping the two calls distinct means a provider can apply the right
prefix without every caller having to remember.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["EmbeddingProviderProtocol", "EmbeddingResult"]


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """Vectors plus the provenance needed to record them against a collection."""

    vectors: tuple[tuple[float, ...], ...]
    model: str
    dimensions: int
    # Upstream revision or weights hash, so a silently republished model is
    # distinguishable from the version that produced the stored index.
    model_revision: str | None = None

    def __len__(self) -> int:
        return len(self.vectors)


@runtime_checkable
class EmbeddingProviderProtocol(Protocol):
    """Turns text into vectors."""

    @property
    def model(self) -> str:
        """Model identifier, recorded on the collection."""
        ...

    @property
    def dimensions(self) -> int:
        """Vector width. Selects the pgvector storage table."""
        ...

    @property
    def model_revision(self) -> str | None:
        """Weights revision, when the provider can report one."""
        ...

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed corpus text for storage."""
        ...

    async def embed_query(self, text: str) -> tuple[float, ...]:
        """Embed a user question for search."""
        ...

    async def health(self) -> tuple[bool, str]:
        """Whether the provider can serve requests, and why not if it cannot."""
        ...
