"""Address parsing and municipality detection.

The ambiguity rule from :mod:`app.domain.municipalities` has to survive into
address handling, because a geocoder would happily resolve "Langley" to one of
the two and the resulting sign rules would be from the wrong jurisdiction.
"""

from __future__ import annotations

import pytest

from app.services.address import AddressOutcome, AddressParser


@pytest.fixture
def parser() -> AddressParser:
    return AddressParser()


class TestResolves:
    def test_simple_address(self, parser: AddressParser) -> None:
        result = parser.parse("123 Main Street, Burnaby")

        assert result.outcome is AddressOutcome.RESOLVED
        assert result.municipality is not None
        assert result.municipality.slug == "burnaby"
        assert result.street_address == "123 Main Street"

    def test_province_and_postal_code_are_stripped(self, parser: AddressParser) -> None:
        result = parser.parse("456 Granville St, Vancouver, BC V6C 1T1")

        assert result.outcome is AddressOutcome.RESOLVED
        assert result.municipality is not None
        assert result.municipality.slug == "vancouver"
        assert "V6C" not in result.street_address

    def test_street_named_after_a_city_is_not_the_city(
        self, parser: AddressParser
    ) -> None:
        """"123 Burnaby Street, Vancouver" is in Vancouver.

        Canadian address order puts the municipality last, so the trailing match
        wins — and the street name must survive into the address handed to the
        zoning provider.
        """
        result = parser.parse("123 Burnaby Street, Vancouver")

        assert result.municipality is not None
        assert result.municipality.slug == "vancouver"
        assert "Burnaby" in result.street_address

    def test_north_vancouver_is_not_vancouver(self, parser: AddressParser) -> None:
        result = parser.parse("100 Esplanade, City of North Vancouver")

        assert result.outcome is AddressOutcome.RESOLVED
        assert result.municipality is not None
        assert result.municipality.slug == "north-vancouver-city"

    def test_province_is_reported(self, parser: AddressParser) -> None:
        result = parser.parse("123 Main Street, Surrey")
        assert result.province_code == "BC"


class TestRefusesToGuess:
    def test_bare_langley_is_ambiguous(self, parser: AddressParser) -> None:
        """The City and the Township have separate bylaws.

        Picking one produces a wrong answer that looks entirely plausible, which
        is the failure this system exists to prevent.
        """
        result = parser.parse("123 Main Street, Langley")

        assert result.outcome is AddressOutcome.AMBIGUOUS_MUNICIPALITY
        assert set(result.candidates) == {"City of Langley", "Township of Langley"}
        assert result.municipality is None

    def test_qualified_langley_resolves(self, parser: AddressParser) -> None:
        result = parser.parse("123 Main Street, Township of Langley")

        assert result.outcome is AddressOutcome.RESOLVED
        assert result.municipality is not None
        assert result.municipality.slug == "langley-township"

    def test_no_municipality_is_reported(self, parser: AddressParser) -> None:
        result = parser.parse("123 Main Street")
        assert result.outcome is AddressOutcome.NO_MUNICIPALITY

    def test_street_without_a_number_is_rejected(self, parser: AddressParser) -> None:
        """A zoning lookup needs a parcel, and a parcel needs a civic number.

        Sending a bare street name to a provider and taking whichever parcel
        comes back first would attach sign rules to an arbitrary property.
        """
        result = parser.parse("Main Street, Burnaby")

        assert result.outcome is AddressOutcome.NO_CIVIC_NUMBER
        assert result.municipality is not None  # the city was still recognised

    def test_empty_input(self, parser: AddressParser) -> None:
        assert parser.parse("   ").outcome is AddressOutcome.NO_CIVIC_NUMBER

    def test_unknown_municipality_is_not_forced(self, parser: AddressParser) -> None:
        result = parser.parse("123 Main Street, Springfield")
        assert result.outcome is AddressOutcome.NO_MUNICIPALITY


class TestUnitNumbers:
    @pytest.mark.parametrize(
        "address",
        ["101-123 Main Street, Burnaby", "1230A Main Street, Burnaby"],
    )
    def test_unit_and_suffixed_numbers_are_accepted(
        self, parser: AddressParser, address: str
    ) -> None:
        assert parser.parse(address).outcome is AddressOutcome.RESOLVED
