"""Provider construction from configuration.

The property under test: adding a municipality is data. A kind, an endpoint and
a field mapping produce a working provider with no Python change — and anything
incomplete produces ``None`` rather than a provider that half-works.
"""

from __future__ import annotations

import pytest

from app.services.zoning.base import normalize_address
from app.services.zoning.presets import PRESETS, preset_for
from app.services.zoning.providers import PROVIDER_KINDS, build_provider

_ARCGIS = {"fields": {"zoning_code": "ZONE", "address": "CIVIC_ADDRESS"}}
_SOCRATA = {"dataset": "zoning", "fields": {"zoning_code": "zone", "address": "address"}}


class TestConstruction:
    def test_arcgis_from_configuration(self) -> None:
        provider = build_provider(
            "arcgis", endpoint="https://gis.example.ca/0", config=_ARCGIS, name="testville"
        )
        assert provider is not None
        assert provider.name == "testville"

    def test_socrata_from_configuration(self) -> None:
        provider = build_provider(
            "socrata", endpoint="https://data.example.ca", config=_SOCRATA, name="calgary"
        )
        assert provider is not None

    def test_a_new_municipality_needs_no_code(self) -> None:
        """The expansion claim, tested.

        Edmonton appears nowhere in the codebase as a module or a branch.
        """
        provider = build_provider(
            "socrata",
            endpoint="https://data.edmonton.ca",
            config=_SOCRATA,
            name="ab-edmonton",
        )
        assert provider is not None
        assert provider.name == "ab-edmonton"


class TestRefusesToHalfWork:
    def test_unknown_kind(self) -> None:
        assert build_provider("carrier-pigeon", endpoint="https://x", config={}) is None

    def test_missing_endpoint(self) -> None:
        assert build_provider("arcgis", endpoint=None, config=_ARCGIS) is None

    def test_missing_kind(self) -> None:
        assert build_provider(None, endpoint="https://x", config=_ARCGIS) is None

    def test_missing_required_field_mapping(self) -> None:
        """An incomplete mapping yields nothing, not a partial provider.

        A provider built without an address field would query with a null
        column name and match whatever the layer returned first.
        """
        assert (
            build_provider("arcgis", endpoint="https://gis.example.ca/0", config={"fields": {}})
            is None
        )

    def test_socrata_without_a_dataset(self) -> None:
        assert (
            build_provider(
                "socrata",
                endpoint="https://data.example.ca",
                config={"fields": {"zoning_code": "z", "address": "a"}},
            )
            is None
        )


class TestPresets:
    def test_every_preset_names_a_known_kind(self) -> None:
        for slug, preset in PRESETS.items():
            assert preset["kind"] in PROVIDER_KINDS, slug

    def test_no_preset_is_marked_verified(self) -> None:
        """Verification is an act, not a default.

        A preset asserting itself verified would let an unchecked endpoint go
        live — and an endpoint carrying a similar-looking field returns a
        confidently wrong zone.
        """
        assert not any(preset["verified"] for preset in PRESETS.values())

    def test_unverified_presets_have_no_endpoint_unless_confirmed(self) -> None:
        """Only entries whose dataset was actually confirmed carry an endpoint."""
        for slug, preset in PRESETS.items():
            if preset["endpoint"] is not None:
                assert preset["notes"], slug

    def test_lookup(self) -> None:
        assert preset_for("burnaby") is not None
        assert preset_for("atlantis") is None


class TestAddressNormalisation:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("123 Main Street", "123 main st"),
            ("456 Granville Avenue", "456 GRANVILLE AVE"),
            ("789 West 4th Road", "789 W 4th Rd"),
            ("1 Côté Boulevard", "1 Cote Blvd"),
        ],
    )
    def test_spellings_collapse_to_one_key(self, left: str, right: str) -> None:
        """Two spellings of one address must not become two cached answers."""
        assert normalize_address(left) == normalize_address(right)

    def test_different_addresses_stay_different(self) -> None:
        assert normalize_address("12 Main St") != normalize_address("112 Main St")
