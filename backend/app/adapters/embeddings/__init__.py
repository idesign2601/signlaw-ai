"""Embedding providers.

Local-first: the default path loads BGE-M3 from the mounted models volume and
never contacts a network service. ``build_embedding_provider`` is the only place
that maps configuration to an implementation.
"""

from __future__ import annotations

from app.adapters.embeddings.base import EmbeddingProviderProtocol, EmbeddingResult
from app.adapters.embeddings.local import LocalEmbeddingProvider
from app.core.config import EmbeddingProvider, EmbeddingSettings
from app.core.exceptions import ConfigurationError

__all__ = [
    "EmbeddingProviderProtocol",
    "EmbeddingResult",
    "LocalEmbeddingProvider",
    "build_embedding_provider",
]


def build_embedding_provider(
    settings: EmbeddingSettings, *, cache_dir: str | None = None
) -> EmbeddingProviderProtocol:
    """Construct the configured provider.

    Raises:
        ConfigurationError: The configured provider has no implementation.
    """
    from pathlib import Path

    if settings.provider is EmbeddingProvider.LOCAL:
        return LocalEmbeddingProvider(
            model=settings.model,
            dimensions=settings.dimensions,
            device=settings.device,
            batch_size=settings.batch_size,
            normalize=settings.normalize,
            cache_dir=Path(cache_dir) if cache_dir else None,
        )

    raise ConfigurationError(
        f"EMBEDDING__PROVIDER={settings.provider.value} has no implementation. "
        "This deployment is local-first; use EMBEDDING__PROVIDER=local."
    )
