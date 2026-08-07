"""Province catalogue.

The catalogue exists so coverage is data-driven. These tests pin the properties
that make adding a province a data change rather than a code change.
"""

from __future__ import annotations

from app.domain.municipalities import MunicipalityRegistry
from app.domain.provinces import PROVINCES, find_municipality, find_province


class TestCatalogue:
    def test_every_province_has_a_unique_code(self) -> None:
        codes = [province.code for province in PROVINCES]
        assert len(codes) == len(set(codes))

    def test_slugs_are_unique_across_provinces(self) -> None:
        """Municipality names repeat across Canada.

        There is a Victoria in BC and a Victoria in PEI. A bare slug would
        collide and route a question to the wrong province's bylaw — the same
        class of failure as the two Langleys, one level up.
        """
        slugs = [
            municipality.slug
            for province in PROVINCES
            for municipality in province.municipalities
        ]
        assert len(slugs) == len(set(slugs))

    def test_alberta_slugs_are_namespaced(self) -> None:
        alberta = find_province("AB")
        assert alberta is not None
        assert all(
            municipality.slug.startswith("ab-")
            for municipality in alberta.municipalities
        )

    def test_province_lookup_is_case_insensitive(self) -> None:
        assert find_province("bc") is not None
        assert find_province("  Bc  ") is not None
        assert find_province("ZZ") is None


class TestMunicipalityLookup:
    def test_returns_the_owning_province(self) -> None:
        found = find_municipality("burnaby")
        assert found is not None
        province, municipality = found
        assert province.code == "BC"
        assert municipality.name == "Burnaby"

    def test_unknown_slug_returns_none(self) -> None:
        assert find_municipality("atlantis") is None

    def test_both_langleys_are_addressable_separately(self) -> None:
        city = find_municipality("langley-city")
        township = find_municipality("langley-township")

        assert city is not None and township is not None
        assert city[1].official_name == "City of Langley"
        assert township[1].official_name == "Township of Langley"


class TestResolutionIsUnaffected:
    def test_calgary_does_not_resolve_during_routing(self) -> None:
        """Catalogued for display is not the same as answerable.

        The resolution registry stays BC-only on purpose. A Calgary question
        should fall through to "no relevant bylaw" rather than resolve to a
        municipality with nothing indexed, which would look like a confident
        miss rather than an absence of coverage.
        """
        registry = MunicipalityRegistry()
        assert registry.resolve("Calgary") is None
