"""Municipality resolution.

Answering a Burnaby question from a Surrey bylaw is the worst failure this
system can produce, and it looks entirely plausible to the reader. These tests
pin the rule that makes it impossible: an ambiguous name resolves to nothing.
"""

from __future__ import annotations

import pytest

from app.domain.municipalities import (
    BC_MUNICIPALITIES,
    MunicipalityClass,
    MunicipalityRegistry,
    slugify,
)


@pytest.fixture
def registry() -> MunicipalityRegistry:
    return MunicipalityRegistry()


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Coquitlam", "coquitlam"),
            ("City of Coquitlam", "coquitlam"),
            ("CITY OF COQUITLAM", "coquitlam"),
            ("The Corporation of Delta", "delta"),
            ("District of North Vancouver", "north-vancouver"),
            ("Township of Langley", "langley"),
            ("Coquitlam, B.C.", "coquitlam"),
            ("Vancouver, British Columbia", "vancouver"),
            ("Maple Ridge", "maple-ridge"),
            ("  Surrey  ", "surrey"),
        ],
    )
    def test_normalisation(self, raw: str, expected: str) -> None:
        assert slugify(raw) == expected

    def test_accents_are_stripped(self) -> None:
        assert slugify("Montréal") == "montreal"

    def test_empty_input(self) -> None:
        assert slugify("") == ""


class TestResolution:
    @pytest.mark.parametrize("name", ["Coquitlam", "City of Coquitlam", "COQUITLAM", "coquitlam"])
    def test_common_forms_resolve(self, registry: MunicipalityRegistry, name: str) -> None:
        record = registry.resolve(name)
        assert record is not None
        assert record.slug == "coquitlam"

    def test_unknown_name_resolves_to_nothing(self, registry: MunicipalityRegistry) -> None:
        assert registry.resolve("Atlantis") is None

    def test_empty_input(self, registry: MunicipalityRegistry) -> None:
        assert registry.resolve("") is None
        assert registry.resolve("   ") is None

    def test_alias_resolves(self, registry: MunicipalityRegistry) -> None:
        assert registry.resolve("New West") is registry.resolve("New Westminster")

    def test_every_example_city_is_present(self, registry: MunicipalityRegistry) -> None:
        # The cities named in the project brief.
        for name in ("Coquitlam", "Burnaby", "Surrey", "Richmond", "Vancouver"):
            assert registry.resolve(name) is not None


class TestAmbiguity:
    """Two Langleys, two North Vancouvers, separate bylaws each."""

    @pytest.mark.parametrize("name", ["Langley", "North Vancouver"])
    def test_bare_ambiguous_name_resolves_to_nothing(
        self, registry: MunicipalityRegistry, name: str
    ) -> None:
        assert registry.resolve(name) is None
        assert registry.is_ambiguous(name)

    def test_candidates_expose_both_options(self, registry: MunicipalityRegistry) -> None:
        candidates = registry.candidates("Langley")
        assert len(candidates) == 2
        assert {record.classification for record in candidates} == {
            MunicipalityClass.CITY,
            MunicipalityClass.TOWNSHIP,
        }

    def test_qualified_form_resolves_cleanly(self, registry: MunicipalityRegistry) -> None:
        city = registry.resolve("City of Langley")
        township = registry.resolve("Township of Langley")
        assert city is not None and township is not None
        assert city.slug != township.slug

    def test_explicit_slug_resolves(self, registry: MunicipalityRegistry) -> None:
        record = registry.resolve("langley-township")
        assert record is not None
        assert record.classification is MunicipalityClass.TOWNSHIP

    def test_unknown_name_is_not_ambiguous(self, registry: MunicipalityRegistry) -> None:
        # Unknown and ambiguous are different problems with different fixes.
        assert not registry.is_ambiguous("Atlantis")
        assert registry.candidates("Atlantis") == ()


class TestFindInText:
    def test_finds_a_single_municipality(self, registry: MunicipalityRegistry) -> None:
        found = registry.find_in_text("City of Coquitlam Sign Bylaw No. 4451")
        assert [record.slug for record in found] == ["coquitlam"]

    def test_longest_match_wins(self, registry: MunicipalityRegistry) -> None:
        # "North Vancouver" must not be read as "Vancouver".
        found = registry.find_in_text("District of North Vancouver Sign Bylaw")
        slugs = [record.slug for record in found]
        assert "north-vancouver-district" in slugs
        assert "vancouver" not in slugs

    def test_new_westminster_is_not_westminster(self, registry: MunicipalityRegistry) -> None:
        found = registry.find_in_text("City of New Westminster Sign Bylaw")
        assert [record.slug for record in found] == ["new-westminster"]

    def test_multiple_municipalities_are_all_reported(self, registry: MunicipalityRegistry) -> None:
        found = registry.find_in_text("Comparing Surrey and Richmond regulations")
        assert {record.slug for record in found} == {"surrey", "richmond"}

    def test_no_match_returns_empty(self, registry: MunicipalityRegistry) -> None:
        assert registry.find_in_text("a document with no place names") == ()

    def test_filename_style_input(self, registry: MunicipalityRegistry) -> None:
        found = registry.find_in_text("burnaby_sign_bylaw_13743.pdf")
        assert [record.slug for record in found] == ["burnaby"]


class TestRegistryData:
    def test_slugs_are_unique(self) -> None:
        slugs = [record.slug for record in BC_MUNICIPALITIES]
        assert len(slugs) == len(set(slugs))

    def test_official_names_carry_the_classification(self) -> None:
        registry = MunicipalityRegistry()
        coquitlam = registry.resolve("Coquitlam")
        assert coquitlam is not None
        assert coquitlam.official_name == "City of Coquitlam"

    def test_every_record_has_a_region(self) -> None:
        assert all(record.region for record in BC_MUNICIPALITIES)

    def test_registry_is_not_trivially_small(self) -> None:
        # Guards against an accidental truncation of the seed data.
        assert len(MunicipalityRegistry()) > 100

    def test_get_by_slug(self) -> None:
        registry = MunicipalityRegistry()
        assert registry.get("coquitlam") is not None
        assert registry.get("nonexistent") is None
