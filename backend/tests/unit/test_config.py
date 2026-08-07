"""Configuration validation.

These tests encode the boot-time invariants. A misconfiguration must fail at
startup with an actionable message, never silently at request time — and never
in a way that lets an unsafe production process serve traffic.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import (
    DatabaseSettings,
    EmbeddingProvider,
    EmbeddingSettings,
    Environment,
    IngestionSettings,
    LLMProvider,
    LLMSettings,
    LogFormat,
    RedisSettings,
    RetrievalSettings,
    SecuritySettings,
    Settings,
    VectorStoreSettings,
    get_settings,
)


class TestDefaults:
    def test_defaults_are_local_first(self, settings_factory) -> None:
        settings = settings_factory()
        assert settings.llm.provider is LLMProvider.OLLAMA
        assert settings.embedding.provider is EmbeddingProvider.LOCAL
        assert settings.environment is Environment.LOCAL

    def test_defaults_require_no_api_keys(self, settings_factory) -> None:
        settings = settings_factory()
        assert settings.llm.openai_api_key is None
        assert settings.embedding.openai_api_key is None

    def test_generation_is_deterministic_by_default(self, settings_factory) -> None:
        # Bylaw answers should not vary between identical runs.
        assert settings_factory().llm.temperature == 0.0

    def test_retrieval_defaults_to_in_force_only(self, settings_factory) -> None:
        # The single most important retrieval default: never surface repealed law.
        assert settings_factory().retrieval.in_force_only is True


class TestDatabaseSettings:
    def test_async_url_uses_asyncpg(self) -> None:
        db = DatabaseSettings(host="db", port=5433, user="u", name="n")
        assert db.async_url.startswith("postgresql+asyncpg://")
        assert "@db:5433/n" in db.async_url

    def test_password_special_characters_are_escaped(self) -> None:
        db = DatabaseSettings(password="p@ss/word:1")  # type: ignore[arg-type]
        assert "p%40ss%2Fword%3A1" in db.async_url

    def test_safe_url_redacts_password(self) -> None:
        db = DatabaseSettings(password="supersecret")  # type: ignore[arg-type]
        assert "supersecret" not in db.safe_url
        assert "***" in db.safe_url

    def test_port_must_be_in_range(self) -> None:
        with pytest.raises(ValidationError):
            DatabaseSettings(port=70000)


class TestRedisSettings:
    def test_url_without_password(self) -> None:
        assert RedisSettings(host="r", port=6380, db=2).url == "redis://r:6380/2"

    def test_safe_url_redacts_password(self) -> None:
        redis = RedisSettings(password="hunter2")  # type: ignore[arg-type]
        assert "hunter2" not in redis.safe_url


class TestLLMSettings:
    def test_openai_provider_requires_key(self) -> None:
        with pytest.raises(ValidationError, match="LLM__OPENAI_API_KEY"):
            LLMSettings(provider=LLMProvider.OPENAI)

    def test_anthropic_provider_requires_key(self) -> None:
        with pytest.raises(ValidationError, match="LLM__ANTHROPIC_API_KEY"):
            LLMSettings(provider=LLMProvider.ANTHROPIC)

    def test_openai_provider_accepts_key(self) -> None:
        settings = LLMSettings(provider=LLMProvider.OPENAI, openai_api_key="sk-test")  # type: ignore[arg-type]
        assert settings.provider is LLMProvider.OPENAI

    def test_ollama_needs_no_key(self) -> None:
        assert LLMSettings(provider=LLMProvider.OLLAMA).openai_api_key is None

    def test_model_is_free_text(self) -> None:
        # Model identifiers are configuration, never hard-coded, so an unknown
        # string must be accepted and validated against the provider at boot.
        assert LLMSettings(model="some-future-model").model == "some-future-model"


class TestEmbeddingSettings:
    def test_openai_provider_requires_key(self) -> None:
        with pytest.raises(ValidationError, match="EMBEDDING__OPENAI_API_KEY"):
            EmbeddingSettings(provider=EmbeddingProvider.OPENAI, model="text-embedding-3-small")

    def test_dimension_mismatch_is_rejected(self) -> None:
        # Catching this at boot avoids a multi-hour index producing bad vectors.
        with pytest.raises(ValidationError, match="does not match the known"):
            EmbeddingSettings(model="BAAI/bge-m3", dimensions=768)

    def test_known_model_dimensions_pass(self) -> None:
        assert EmbeddingSettings(model="BAAI/bge-m3", dimensions=1024).dimensions == 1024

    def test_unknown_model_skips_dimension_check(self) -> None:
        # An unlisted model is accepted, but its width must still have a
        # pgvector table or the vectors have nowhere to go.
        settings = EmbeddingSettings(model="acme/custom-embedder", dimensions=768)
        assert settings.dimensions == 768

    def test_dimension_without_a_storage_table_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="no pgvector storage table"):
            EmbeddingSettings(model="acme/custom-embedder", dimensions=999)

    @pytest.mark.parametrize("device", ["auto", "cpu", "cuda", "cuda:1", "MPS"])
    def test_valid_devices(self, device: str) -> None:
        assert EmbeddingSettings(device=device).device == device.lower()

    def test_invalid_device_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="EMBEDDING__DEVICE"):
            EmbeddingSettings(device="tpu")


class TestVectorStoreSettings:
    # Collection naming moved to app.rag.collections.CollectionSpec when the
    # vector store became pgvector: the name is `signlaw_bge_m3_v1`, which needs
    # the embedding model, and VectorStoreSettings does not know it. Naming is
    # covered by tests/unit/test_collections.py.

    def test_pgvector_operator_matches_the_metric(self) -> None:
        assert VectorStoreSettings(distance_metric="cosine").pgvector_operator == "<=>"
        assert VectorStoreSettings(distance_metric="l2").pgvector_operator == "<->"

    def test_pgvector_ops_class_matches_the_metric(self) -> None:
        # Must match the operator class the HNSW index was built with, or the
        # index is silently not used.
        assert (
            VectorStoreSettings(distance_metric="cosine").pgvector_ops_class
            == "vector_cosine_ops"
        )

    def test_invalid_prefix_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="COLLECTION_PREFIX"):
            VectorStoreSettings(collection_prefix="bad prefix!")

    def test_invalid_metric_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="DISTANCE_METRIC"):
            VectorStoreSettings(distance_metric="manhattan")


class TestIngestionSettings:
    def test_overlap_must_be_smaller_than_target(self) -> None:
        # Otherwise the chunker cannot advance and would loop forever.
        with pytest.raises(ValidationError, match="CHUNK_OVERLAP_TOKENS"):
            IngestionSettings(chunk_target_tokens=500, chunk_overlap_tokens=500)

    def test_max_must_not_be_below_target(self) -> None:
        with pytest.raises(ValidationError, match="CHUNK_MAX_TOKENS"):
            IngestionSettings(chunk_target_tokens=900, chunk_max_tokens=800)

    def test_min_must_be_smaller_than_target(self) -> None:
        with pytest.raises(ValidationError, match="CHUNK_MIN_TOKENS"):
            IngestionSettings(chunk_target_tokens=200, chunk_min_tokens=200)

    def test_max_file_size_bytes_conversion(self) -> None:
        assert IngestionSettings(max_file_size_mb=2).max_file_size_bytes == 2 * 1024 * 1024


class TestRetrievalSettings:
    def test_rerank_top_n_cannot_exceed_candidate_pool(self) -> None:
        with pytest.raises(ValidationError, match="RERANK_TOP_N"):
            RetrievalSettings(dense_top_k=5, sparse_top_k=5, rerank_top_n=20)

    def test_valid_configuration_passes(self) -> None:
        assert RetrievalSettings(dense_top_k=20, sparse_top_k=20, rerank_top_n=8).rerank_top_n == 8


class TestSecuritySettings:
    def test_short_admin_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least 32 characters"):
            SecuritySettings(admin_api_key="short")  # type: ignore[arg-type]

    def test_long_admin_key_is_accepted(self) -> None:
        assert SecuritySettings(admin_api_key="a" * 64).admin_api_key is not None  # type: ignore[arg-type]

    def test_admin_key_is_optional(self) -> None:
        assert SecuritySettings().admin_api_key is None


class TestProductionInvariants:
    """Production must not boot in a configuration that risks data or law."""

    def _production_kwargs(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "environment": Environment.PRODUCTION,
            "debug": False,
            "db": DatabaseSettings(password="a-real-production-password"),  # type: ignore[arg-type]
            # api_keys as well as admin_api_key: an unauthenticated /ask is an
            # open GPU inference endpoint, so production refuses to boot
            # without it just as it does without the admin key.
            "security": SecuritySettings(admin_api_key="k" * 64, api_keys=["c" * 64], cors_origins=["https://app.example.com"]),  # type: ignore[arg-type]
            "observability": {"log_format": LogFormat.JSON},
        }
        base.update(overrides)
        return base

    def test_valid_production_config_boots(self, settings_factory) -> None:
        settings = settings_factory(**self._production_kwargs())
        assert settings.environment.is_production

    def test_debug_is_rejected(self, settings_factory) -> None:
        with pytest.raises(ValidationError, match="DEBUG must be false"):
            settings_factory(**self._production_kwargs(debug=True))

    def test_missing_admin_key_is_rejected(self, settings_factory) -> None:
        with pytest.raises(ValidationError, match="ADMIN_API_KEY"):
            settings_factory(
                **self._production_kwargs(
                    security=SecuritySettings(cors_origins=["https://app.example.com"])
                )
            )

    def test_default_db_password_is_rejected(self, settings_factory) -> None:
        with pytest.raises(ValidationError, match="DB__PASSWORD"):
            settings_factory(**self._production_kwargs(db=DatabaseSettings(password="signlaw")))  # type: ignore[arg-type]

    def test_wildcard_cors_is_rejected(self, settings_factory) -> None:
        with pytest.raises(ValidationError, match="CORS_ORIGINS"):
            settings_factory(
                **self._production_kwargs(
                    security=SecuritySettings(admin_api_key="k" * 64, cors_origins=["*"])  # type: ignore[arg-type]
                )
            )

    def test_console_logging_is_rejected(self, settings_factory) -> None:
        with pytest.raises(ValidationError, match="LOG_FORMAT"):
            settings_factory(
                **self._production_kwargs(observability={"log_format": LogFormat.CONSOLE})
            )

    def test_all_problems_are_reported_together(self, settings_factory) -> None:
        # One boot, one complete list of what to fix.
        with pytest.raises(ValidationError) as exc_info:
            settings_factory(
                environment=Environment.PRODUCTION,
                debug=True,
                db=DatabaseSettings(password="signlaw"),  # type: ignore[arg-type]
            )
        message = str(exc_info.value)
        assert "DEBUG must be false" in message
        assert "ADMIN_API_KEY" in message
        assert "API_KEYS" in message
        assert "DB__PASSWORD" in message

    def test_non_production_is_permissive(self, settings_factory) -> None:
        settings = settings_factory(environment=Environment.LOCAL, debug=True)
        assert settings.debug is True


class TestApiPrefix:
    def test_prefix_must_be_absolute(self, settings_factory) -> None:
        with pytest.raises(ValidationError, match="API_PREFIX"):
            settings_factory(api_prefix="v1")

    def test_trailing_slash_is_stripped(self, settings_factory) -> None:
        assert settings_factory(api_prefix="/api/v1/").api_prefix == "/api/v1"


class TestSettingsCache:
    def test_get_settings_is_cached(self) -> None:
        assert get_settings() is get_settings()

    def test_cache_clear_rebuilds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = get_settings()
        get_settings.cache_clear()
        monkeypatch.setenv("APP_NAME", "Renamed")
        second = get_settings()
        assert first is not second
        assert second.app_name == "Renamed"


class TestEnvironmentParsing:
    def test_nested_delimiter_maps_to_submodel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB__HOST", "postgres.internal")
        monkeypatch.setenv("LLM__MODEL", "qwen2.5:32b-instruct")
        get_settings.cache_clear()

        settings = Settings()
        assert settings.db.host == "postgres.internal"
        assert settings.llm.model == "qwen2.5:32b-instruct"

    def test_unknown_variables_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOMETHING_UNRELATED", "value")
        get_settings.cache_clear()
        assert Settings().app_name
