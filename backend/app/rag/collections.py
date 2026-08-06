"""Versioned embedding collections.

A collection is one complete index over the corpus, identified by three axes
that each independently invalidate stored vectors:

    signlaw_bge_m3_v1
    ^prefix ^model     ^index version
            (+ chunking version, tracked but not in the name)

**Why rebuilding is additive.** A new collection is populated to ``BUILDING``
while the ``ACTIVE`` one keeps serving. Activation is a single transaction that
retires the old and promotes the new, so there is never a window where the index
is half-rebuilt, and rollback is one UPDATE.

**Why a model change is cheap.** Extraction, OCR, table detection and section
parsing all persist in Postgres and are untouched by re-embedding. Changing the
embedding model re-runs only::

    chunks -> embeddings -> index

Changing the *chunking* version is the more expensive case, and is tracked
separately so the two are never confused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["CollectionSpec", "model_slug"]

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def model_slug(model: str) -> str:
    """Reduce a model identifier to a name-safe fragment.

    ``BAAI/bge-m3`` becomes ``bge_m3``. The organisation prefix is dropped
    because it carries no information that distinguishes one vector space from
    another, and it makes collection names unwieldy.
    """
    tail = model.rsplit("/", 1)[-1].lower()
    slug = _SLUG_STRIP.sub("_", tail).strip("_")
    return slug or "model"


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    """The identity of one index build."""

    prefix: str
    embedding_model: str
    dimensions: int
    index_version: int
    chunking_version: int = 1
    distance_metric: str = "cosine"
    embedding_model_revision: str | None = None

    def __post_init__(self) -> None:
        if self.index_version < 1:
            raise ValueError("index_version must be >= 1")
        if self.chunking_version < 1:
            raise ValueError("chunking_version must be >= 1")
        if self.dimensions < 1:
            raise ValueError("dimensions must be >= 1")

    @property
    def name(self) -> str:
        """Collection name, e.g. ``signlaw_bge_m3_v1``."""
        return f"{self.prefix}_{model_slug(self.embedding_model)}_v{self.index_version}"

    @property
    def table_name(self) -> str:
        """pgvector table holding this collection's vectors."""
        return f"chunk_embedding_{self.dimensions}"

    def next_index_version(self) -> CollectionSpec:
        """The spec for rebuilding this configuration from scratch."""
        return CollectionSpec(
            prefix=self.prefix,
            embedding_model=self.embedding_model,
            dimensions=self.dimensions,
            index_version=self.index_version + 1,
            chunking_version=self.chunking_version,
            distance_metric=self.distance_metric,
            embedding_model_revision=self.embedding_model_revision,
        )

    def with_model(self, model: str, dimensions: int) -> CollectionSpec:
        """The spec for the same corpus under a different embedding model.

        Index version resets to 1: this is a different vector space, not a
        rebuild of the current one.
        """
        return CollectionSpec(
            prefix=self.prefix,
            embedding_model=model,
            dimensions=dimensions,
            index_version=1,
            chunking_version=self.chunking_version,
            distance_metric=self.distance_metric,
        )

    def shares_chunks_with(self, other: CollectionSpec) -> bool:
        """Whether both collections index the same chunk text.

        When true, a rebuild only needs to re-embed — chunks, sections, pages
        and OCR output are all reusable. This is the check that makes swapping
        embedding model cheap.
        """
        return self.chunking_version == other.chunking_version
