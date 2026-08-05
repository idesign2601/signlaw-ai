"""Versioned embedding collections.

Two properties matter. Collection names must encode enough to tell two
incompatible vector spaces apart, and the version axes must distinguish a
cheap rebuild (new embedding model) from an expensive one (new chunking).
"""

from __future__ import annotations

import pytest

from app.rag.collections import CollectionSpec, model_slug


def spec(**overrides: object) -> CollectionSpec:
    defaults: dict[str, object] = {
        "prefix": "signlaw",
        "embedding_model": "BAAI/bge-m3",
        "dimensions": 1024,
        "index_version": 1,
        "chunking_version": 1,
    }
    defaults.update(overrides)
    return CollectionSpec(**defaults)  # type: ignore[arg-type]


class TestModelSlug:
    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("BAAI/bge-m3", "bge_m3"),
            ("BAAI/bge-large-en-v1.5", "bge_large_en_v1_5"),
            ("intfloat/multilingual-e5-large", "multilingual_e5_large"),
            ("all-MiniLM-L6-v2", "all_minilm_l6_v2"),
        ],
    )
    def test_slugs(self, model: str, expected: str) -> None:
        assert model_slug(model) == expected

    def test_organisation_prefix_is_dropped(self) -> None:
        # It carries nothing that distinguishes one vector space from another.
        assert model_slug("BAAI/bge-m3") == model_slug("someone-else/bge-m3")

    def test_degenerate_input(self) -> None:
        assert model_slug("///") == "model"


class TestNaming:
    def test_matches_the_documented_format(self) -> None:
        assert spec().name == "signlaw_bge_m3_v1"

    def test_index_version_appears_in_the_name(self) -> None:
        assert spec(index_version=2).name == "signlaw_bge_m3_v2"

    def test_different_models_get_different_names(self) -> None:
        assert spec().name != spec(embedding_model="intfloat/multilingual-e5-large").name

    def test_prefix_is_configurable(self) -> None:
        assert spec(prefix="bylaws").name == "bylaws_bge_m3_v1"

    def test_storage_table_follows_the_dimension(self) -> None:
        assert spec().table_name == "chunk_embedding_1024"
        assert spec(dimensions=768).table_name == "chunk_embedding_768"


class TestVersionAxes:
    def test_rebuild_bumps_only_the_index_version(self) -> None:
        rebuilt = spec(index_version=1).next_index_version()
        assert rebuilt.index_version == 2
        assert rebuilt.embedding_model == "BAAI/bge-m3"
        assert rebuilt.chunking_version == 1

    def test_changing_model_resets_the_index_version(self) -> None:
        # A different vector space, not a rebuild of the current one.
        changed = spec(index_version=5).with_model("BAAI/bge-base-en-v1.5", 768)
        assert changed.index_version == 1
        assert changed.dimensions == 768

    def test_same_chunking_means_only_re_embedding_is_needed(self) -> None:
        # The property that makes swapping embedding model cheap: extraction,
        # OCR, tables and sections are all reusable.
        current = spec(chunking_version=3)
        new_model = current.with_model("BAAI/bge-base-en-v1.5", 768)
        assert new_model.shares_chunks_with(current)

    def test_different_chunking_requires_re_chunking(self) -> None:
        assert not spec(chunking_version=1).shares_chunks_with(spec(chunking_version=2))

    def test_chunking_version_is_not_in_the_name(self) -> None:
        # Tracked on the collection row; the name stays readable.
        assert spec(chunking_version=1).name == spec(chunking_version=9).name


class TestValidation:
    @pytest.mark.parametrize("version", [0, -1])
    def test_index_version_must_be_positive(self, version: int) -> None:
        with pytest.raises(ValueError, match="index_version"):
            spec(index_version=version)

    def test_chunking_version_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="chunking_version"):
            spec(chunking_version=0)

    def test_dimensions_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            spec(dimensions=0)
