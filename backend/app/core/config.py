"""Application configuration.

Every setting is declared here, validated at process start, and reached through
:func:`get_settings`. A misconfiguration therefore fails loudly at boot rather
than on the first request that happens to touch it.

Nested settings use a double-underscore environment delimiter::

    DB__HOST=postgres        -> settings.db.host
    LLM__PROVIDER=ollama     -> settings.llm.provider

Defaults are local-first: Ollama for generation and BGE-M3 for embeddings, so a
clean checkout sends nothing off the machine.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self
from urllib.parse import quote_plus

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "SUPPORTED_EMBEDDING_DIMENSIONS",
    "DatabaseSettings",
    "EmbeddingProvider",
    "EmbeddingSettings",
    "Environment",
    "IngestionSettings",
    "LLMProvider",
    "LLMSettings",
    "LogFormat",
    "ObservabilitySettings",
    "RedisSettings",
    "RetrievalSettings",
    "SecuritySettings",
    "Settings",
    "VectorStoreSettings",
    "get_settings",
]


class Environment(StrEnum):
    """Deployment environment."""

    LOCAL = "local"
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production(self) -> bool:
        return self is Environment.PRODUCTION


class LLMProvider(StrEnum):
    """Supported generation backends."""

    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class EmbeddingProvider(StrEnum):
    """Supported embedding backends."""

    LOCAL = "local"
    OPENAI = "openai"


class LogFormat(StrEnum):
    """Log renderer."""

    CONSOLE = "console"
    JSON = "json"


# Known embedding dimensions. Used to catch a mismatched EMBEDDING__DIMENSIONS
# at boot instead of after a multi-hour indexing run produces unusable vectors.
KNOWN_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "BAAI/bge-m3": 1024,
    "BAAI/bge-large-en-v1.5": 1024,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "intfloat/multilingual-e5-large": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}

# pgvector requires a fixed dimension per column to build an HNSW index, so
# embeddings are stored in one physical table per dimension and routed by the
# collection's dimensionality. Pre-creating the common sizes means swapping
# embedding model is a configuration change, not a migration.
SUPPORTED_EMBEDDING_DIMENSIONS: tuple[int, ...] = (384, 768, 1024, 1536)


class DatabaseSettings(BaseModel):
    """Postgres connection and pool configuration."""

    host: str = "localhost"
    port: int = Field(default=5432, ge=1, le=65535)
    user: str = "signlaw"
    password: SecretStr = SecretStr("signlaw")
    name: str = "signlaw"

    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=200)
    pool_timeout_s: float = Field(default=30.0, gt=0)
    pool_recycle_s: int = Field(default=1800, gt=0)
    pool_pre_ping: bool = True
    echo: bool = False

    connect_timeout_s: int = Field(default=10, gt=0)

    def _dsn(self, driver: str) -> str:
        user = quote_plus(self.user)
        password = quote_plus(self.password.get_secret_value())
        return f"postgresql+{driver}://{user}:{password}@{self.host}:{self.port}/{self.name}"

    @property
    def async_url(self) -> str:
        """DSN for the async engine.

        Alembic uses the same URL and drives migrations through
        ``connection.run_sync``, so the project needs only one Postgres driver.
        """
        return self._dsn("asyncpg")

    @property
    def safe_url(self) -> str:
        """DSN with the password redacted — safe to log."""
        user = quote_plus(self.user)
        return f"postgresql://{user}:***@{self.host}:{self.port}/{self.name}"


class RedisSettings(BaseModel):
    """Redis connection for the ingestion job queue."""

    host: str = "localhost"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0)
    password: SecretStr | None = None

    @property
    def url(self) -> str:
        auth = f":{quote_plus(self.password.get_secret_value())}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"

    @property
    def safe_url(self) -> str:
        auth = ":***@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class LLMSettings(BaseModel):
    """Generation model configuration.

    ``model`` is never hard-coded anywhere else in the codebase: switching
    provider or model is a configuration change only.
    """

    provider: LLMProvider = LLMProvider.OLLAMA
    model: str = "qwen2.5:14b-instruct"

    # Deterministic by default. Bylaw answers should not vary run to run.
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0, le=32768)
    request_timeout_s: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)

    ollama_base_url: str = "http://localhost:11434"

    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    anthropic_api_key: SecretStr | None = None

    # Verify the configured model exists on the provider during startup.
    verify_model_on_startup: bool = True

    @model_validator(mode="after")
    def _require_credentials_for_provider(self) -> Self:
        if self.provider is LLMProvider.OPENAI and self.openai_api_key is None:
            raise ValueError(
                "LLM__OPENAI_API_KEY is required when LLM__PROVIDER=openai. "
                "Set the key, or switch to LLM__PROVIDER=ollama for local generation."
            )
        if self.provider is LLMProvider.ANTHROPIC and self.anthropic_api_key is None:
            raise ValueError(
                "LLM__ANTHROPIC_API_KEY is required when LLM__PROVIDER=anthropic. "
                "Set the key, or switch to LLM__PROVIDER=ollama for local generation."
            )
        return self


class EmbeddingSettings(BaseModel):
    """Embedding model configuration.

    Changing ``model`` or ``dimensions`` invalidates the existing vector index;
    a re-index into a new collection version is required.
    """

    provider: EmbeddingProvider = EmbeddingProvider.LOCAL
    model: str = "BAAI/bge-m3"
    dimensions: int = Field(default=1024, gt=0, le=8192)
    batch_size: int = Field(default=32, ge=1, le=2048)
    max_retries: int = Field(default=3, ge=0, le=10)
    request_timeout_s: float = Field(default=60.0, gt=0)

    # "auto" resolves to cuda when available, else cpu.
    device: str = "auto"
    normalize: bool = True

    openai_api_key: SecretStr | None = None

    @field_validator("device")
    @classmethod
    def _validate_device(cls, value: str) -> str:
        allowed = {"auto", "cpu", "cuda", "mps"}
        normalized = value.strip().lower()
        if normalized not in allowed and not normalized.startswith("cuda:"):
            raise ValueError(f"EMBEDDING__DEVICE must be one of {sorted(allowed)} or 'cuda:N'")
        return normalized

    @model_validator(mode="after")
    def _validate_model_and_dimensions(self) -> Self:
        if self.provider is EmbeddingProvider.OPENAI and self.openai_api_key is None:
            raise ValueError(
                "EMBEDDING__OPENAI_API_KEY is required when EMBEDDING__PROVIDER=openai. "
                "Set the key, or switch to EMBEDDING__PROVIDER=local."
            )

        expected = KNOWN_EMBEDDING_DIMENSIONS.get(self.model)
        if expected is not None and expected != self.dimensions:
            raise ValueError(
                f"EMBEDDING__DIMENSIONS={self.dimensions} does not match the known "
                f"dimensionality of '{self.model}' ({expected}). Fix the dimension, "
                f"or use a model not in the known-dimensions table."
            )

        if self.dimensions not in SUPPORTED_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"EMBEDDING__DIMENSIONS={self.dimensions} has no pgvector storage "
                f"table. Supported: {list(SUPPORTED_EMBEDDING_DIMENSIONS)}. Add a "
                f"migration creating chunk_embedding_{self.dimensions} to use this model."
            )
        return self


class VectorStoreSettings(BaseModel):
    """pgvector configuration.

    Embeddings live in Postgres alongside documents, sections, chunks, citation
    metadata and amendment lineage. One store means a chunk and its vector are
    written in a single transaction and can never drift apart, and a filtered
    search ("in-force bylaws in Coquitlam") is a join rather than a fan-out to a
    second system followed by a reconciliation pass.

    Collections are versioned by *three* independent axes, because any of them
    invalidates existing vectors:

        signlaw_bge_m3_v1
        ^prefix ^model    ^index version

    ``chunking_version`` is tracked on the collection too, so a change to
    chunking rules is distinguishable from a change of embedding model.
    """

    collection_prefix: str = "signlaw"
    index_version: int = Field(default=1, ge=1)
    # Bumped when chunking rules change. A new chunking version requires
    # re-chunking; a new embedding model does not.
    chunking_version: int = Field(default=1, ge=1)

    # Cosine suits normalised text embeddings. Maps to pgvector's
    # vector_cosine_ops operator class.
    distance_metric: str = "cosine"
    upsert_batch_size: int = Field(default=256, ge=1, le=10000)

    # HNSW build parameters. These defaults suit corpora up to a few million
    # chunks; raising them improves recall at the cost of build time.
    hnsw_m: int = Field(default=16, ge=4, le=100)
    hnsw_ef_construction: int = Field(default=64, ge=8, le=1000)
    # Query-time candidate list. Must exceed the requested top-k or recall
    # degrades sharply.
    hnsw_ef_search: int = Field(default=100, ge=8, le=1000)

    @field_validator("collection_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("VECTOR__COLLECTION_PREFIX must be alphanumeric with underscores")
        return value

    @field_validator("distance_metric")
    @classmethod
    def _validate_metric(cls, value: str) -> str:
        allowed = {"cosine", "l2", "ip"}
        if value not in allowed:
            raise ValueError(f"VECTOR__DISTANCE_METRIC must be one of {sorted(allowed)}")
        return value

    @property
    def pgvector_operator(self) -> str:
        """Distance operator for the configured metric."""
        return {"cosine": "<=>", "l2": "<->", "ip": "<#>"}[self.distance_metric]

    @property
    def pgvector_ops_class(self) -> str:
        """Index operator class for the configured metric."""
        return {
            "cosine": "vector_cosine_ops",
            "l2": "vector_l2_ops",
            "ip": "vector_ip_ops",
        }[self.distance_metric]


class IngestionSettings(BaseModel):
    """PDF discovery, extraction, OCR and chunking parameters."""

    corpus_dir: Path = Path("./data/corpus")
    blob_dir: Path = Path("./data/blobs")

    max_file_size_mb: int = Field(default=200, gt=0, le=2048)
    concurrency: int = Field(default=4, ge=1, le=64)

    # A page yielding fewer extractable characters than this is treated as a
    # scan and routed to OCR.
    scan_detection_min_chars: int = Field(default=120, ge=0)
    ocr_enabled: bool = True
    # '+'-separated Tesseract language codes, e.g. "eng" or "eng+fra".
    ocr_languages: str = "eng"
    ocr_dpi: int = Field(default=300, ge=72, le=1200)
    ocr_timeout_s: float = Field(default=600.0, gt=0)
    # Where Tesseract looks for *.traineddata. These are trained models and are
    # deliberately kept out of the Docker image; `make fetch-models` populates
    # this directory on a mounted volume.
    tessdata_dir: Path = Path("./data/models/tessdata")

    chunk_target_tokens: int = Field(default=700, ge=64, le=8192)
    chunk_overlap_tokens: int = Field(default=80, ge=0, le=2048)
    chunk_max_tokens: int = Field(default=1200, ge=64, le=16384)
    # Chunks shorter than this are merged into a neighbour within the same
    # section rather than indexed on their own.
    chunk_min_tokens: int = Field(default=48, ge=0)

    # Only the first N pages are sent to the LLM for metadata detection.
    metadata_llm_page_limit: int = Field(default=2, ge=0, le=10)
    metadata_llm_enabled: bool = True

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @model_validator(mode="after")
    def _validate_chunk_sizes(self) -> Self:
        if self.chunk_overlap_tokens >= self.chunk_target_tokens:
            raise ValueError(
                "INGESTION__CHUNK_OVERLAP_TOKENS must be smaller than "
                "INGESTION__CHUNK_TARGET_TOKENS, otherwise chunking cannot advance."
            )
        if self.chunk_max_tokens < self.chunk_target_tokens:
            raise ValueError(
                "INGESTION__CHUNK_MAX_TOKENS must be >= INGESTION__CHUNK_TARGET_TOKENS."
            )
        if self.chunk_min_tokens >= self.chunk_target_tokens:
            raise ValueError(
                "INGESTION__CHUNK_MIN_TOKENS must be smaller than INGESTION__CHUNK_TARGET_TOKENS."
            )
        return self


class RetrievalSettings(BaseModel):
    """Hybrid retrieval, reranking and abstention thresholds.

    The intended shape of the pipeline::

        pgvector top 50  +  Postgres full-text top 50
                    -> weighted fusion -> top 50
                    -> local cross-encoder rerank -> top 5
                    -> LLM

    ``candidate_pool_size`` is what the reranker sees; ``rerank_top_n`` is what
    reaches the model.
    """

    dense_top_k: int = Field(default=50, ge=1, le=500)
    sparse_top_k: int = Field(default=50, ge=0, le=500)
    # Candidates surviving fusion and handed to the reranker.
    candidate_pool_size: int = Field(default=50, ge=1, le=500)

    # Reciprocal Rank Fusion smoothing constant. Higher values flatten the
    # advantage of top-ranked results from either retriever.
    rrf_k: int = Field(default=60, ge=1)
    # Relative trust in each retriever during fusion. Bylaw queries are full of
    # exact terms ("fascia sign", "Bylaw 4451") that dense embeddings blur, so
    # the sparse side is weighted equally rather than as a tie-breaker.
    dense_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    sparse_weight: float = Field(default=0.5, ge=0.0, le=1.0)

    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_top_n: int = Field(default=5, ge=1, le=50)
    rerank_batch_size: int = Field(default=16, ge=1, le=256)

    min_relevance_score: float = Field(default=0.15, ge=0.0, le=1.0)
    abstain_below_confidence: float = Field(default=0.35, ge=0.0, le=1.0)

    # Retrieve small chunks, then expand to the parent section for context.
    parent_expansion_enabled: bool = True
    max_context_tokens: int = Field(default=12000, ge=512)

    # Exclude documents known to be superseded or repealed.
    in_force_only: bool = True
    # Per-city budget when fanning out a multi-city comparison.
    compare_top_n_per_city: int = Field(default=6, ge=1, le=50)

    @model_validator(mode="after")
    def _validate_pipeline_widths(self) -> Self:
        pool = self.dense_top_k + self.sparse_top_k
        if pool == 0:
            raise ValueError(
                "RETRIEVAL__DENSE_TOP_K and RETRIEVAL__SPARSE_TOP_K cannot both be zero."
            )
        if self.rerank_top_n > pool:
            raise ValueError(
                f"RETRIEVAL__RERANK_TOP_N ({self.rerank_top_n}) exceeds the candidate "
                f"pool of dense_top_k + sparse_top_k ({pool})."
            )
        if self.rerank_top_n > self.candidate_pool_size:
            raise ValueError(
                f"RETRIEVAL__RERANK_TOP_N ({self.rerank_top_n}) exceeds "
                f"RETRIEVAL__CANDIDATE_POOL_SIZE ({self.candidate_pool_size}) — "
                f"the reranker cannot return more than it is given."
            )
        if self.dense_weight + self.sparse_weight <= 0:
            raise ValueError(
                "RETRIEVAL__DENSE_WEIGHT and RETRIEVAL__SPARSE_WEIGHT cannot both be zero."
            )
        return self


class SecuritySettings(BaseModel):
    """Authentication, CORS and rate limiting."""

    # Guards every destructive admin route (re-index, delete).
    admin_api_key: SecretStr | None = None
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    cors_allow_credentials: bool = True
    rate_limit_per_minute: int = Field(default=60, ge=1, le=10000)
    max_request_body_mb: int = Field(default=25, gt=0, le=1024)

    @field_validator("admin_api_key")
    @classmethod
    def _reject_short_keys(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError(
                "SECURITY__ADMIN_API_KEY must be at least 32 characters. "
                "Generate one with: openssl rand -hex 32"
            )
        return value


class ObservabilitySettings(BaseModel):
    """Logging and instrumentation."""

    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.CONSOLE
    log_sql: bool = False
    # Emit token counts and estimated cost per request.
    track_token_usage: bool = True
    # Persist the full retrieval trace for every answer. Required for audit.
    persist_retrieval_trace: bool = True

    @field_validator("log_level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = value.strip().upper()
        if normalized not in allowed:
            raise ValueError(f"OBSERVABILITY__LOG_LEVEL must be one of {sorted(allowed)}")
        return normalized


class Settings(BaseSettings):
    """Root settings object."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
    )

    app_name: str = "SignLaw AI"
    version: str = "0.1.0"
    environment: Environment = Environment.LOCAL
    debug: bool = False

    api_prefix: str = "/api/v1"
    host: str = "0.0.0.0"  # noqa: S104 — binding all interfaces is intended in a container
    port: int = Field(default=8000, ge=1, le=65535)

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    vector: VectorStoreSettings = Field(default_factory=VectorStoreSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @field_validator("api_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("API_PREFIX must start with '/'")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _enforce_production_invariants(self) -> Self:
        """Refuse to boot a production process in an unsafe configuration."""
        if not self.environment.is_production:
            return self

        problems: list[str] = []

        if self.debug:
            problems.append("DEBUG must be false in production")
        if self.security.admin_api_key is None:
            problems.append(
                "SECURITY__ADMIN_API_KEY must be set in production — admin routes "
                "re-index and delete documents"
            )
        if self.db.password.get_secret_value() in {"signlaw", "postgres", "change-me"}:
            problems.append("DB__PASSWORD is a default value")
        if "*" in self.security.cors_origins:
            problems.append("SECURITY__CORS_ORIGINS must not be '*' in production")
        if self.observability.log_format is not LogFormat.JSON:
            problems.append("OBSERVABILITY__LOG_FORMAT should be 'json' in production")

        if problems:
            bullets = "\n  - ".join(problems)
            raise ValueError(f"Unsafe production configuration:\n  - {bullets}")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that validation runs exactly once. Tests that need a different
    configuration should call ``get_settings.cache_clear()`` after patching the
    environment.
    """
    return Settings()
