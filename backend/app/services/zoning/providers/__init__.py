"""Provider construction from configuration.

Three *kinds* of service, because each speaks a different query grammar:

``arcgis``
    ArcGIS REST FeatureServer or MapServer layers. Most BC and Alberta cities.

``opendatasoft``
    Opendatasoft portals. Vancouver, among others.

``socrata``
    Socrata open data. Calgary, among others.

**Cities are not code.** A municipality supplies its kind, its endpoint and a
field mapping in ``gis_config``, and that is the whole of it. Adding Edmonton is
one database row.

:mod:`app.services.zoning.presets` holds known-good configurations for seeding,
but nothing reads them at runtime — they are starting points for an operator,
not a code path.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.services.zoning.base import ZoningProviderProtocol
from app.services.zoning.providers.arcgis import ArcGisFieldMap, ArcGisZoningProvider
from app.services.zoning.providers.opendatasoft import (
    GeocoderProtocol,
    OpendatasoftZoningProvider,
)
from app.services.zoning.providers.socrata import SocrataZoningProvider

__all__ = ["PROVIDER_KINDS", "build_provider"]

logger = get_logger(__name__)

PROVIDER_KINDS = ("arcgis", "opendatasoft", "socrata")


def build_provider(
    kind: str | None,
    *,
    endpoint: str | None = None,
    config: dict[str, Any] | None = None,
    map_url: str | None = None,
    name: str | None = None,
    geocoder: GeocoderProtocol | None = None,
) -> ZoningProviderProtocol | None:
    """Construct a provider from a municipality's configuration.

    Returns ``None`` — rather than raising or guessing — when the kind is
    unknown, the endpoint is missing, or the configuration lacks a field the
    kind requires. The service reports zoning as unavailable for that city,
    which is the honest outcome and the safe one.
    """
    if not kind or not endpoint:
        return None

    settings = config or {}
    provider_name = name or kind
    resolved = kind.strip().lower()

    if resolved not in PROVIDER_KINDS:
        logger.warning("zoning_provider_kind_unknown", kind=kind)
        return None

    try:
        provider: ZoningProviderProtocol
        if resolved == "arcgis":
            provider = _arcgis(provider_name, endpoint, settings, map_url, geocoder)
        elif resolved == "opendatasoft":
            provider = _opendatasoft(
                provider_name, endpoint, settings, map_url, geocoder
            )
        else:
            provider = _socrata(provider_name, endpoint, settings, map_url, geocoder)
    except KeyError as exc:
        # A required field mapping is absent. Named explicitly so an operator
        # can fix the configuration rather than guess at it.
        logger.warning(
            "zoning_provider_misconfigured",
            provider=provider_name,
            kind=kind,
            missing=str(exc).strip("'"),
        )
        return None

    return provider


def _arcgis(
    name: str,
    endpoint: str,
    config: dict[str, Any],
    map_url: str | None,
    _geocoder: GeocoderProtocol | None,
) -> ArcGisZoningProvider:
    fields = config["fields"]
    return ArcGisZoningProvider(
        provider_name=name,
        endpoint=endpoint,
        fields=ArcGisFieldMap(
            zoning_code=fields["zoning_code"],
            address=fields["address"],
            zoning_description=fields.get("zoning_description"),
            parcel_number=fields.get("parcel_number"),
            legal_description=fields.get("legal_description"),
        ),
        public_map_url=map_url,
        extra_params=config.get("extra_params", {}),
    )


def _opendatasoft(
    name: str,
    endpoint: str,
    config: dict[str, Any],
    map_url: str | None,
    geocoder: GeocoderProtocol | None,
) -> OpendatasoftZoningProvider:
    fields = config["fields"]
    return OpendatasoftZoningProvider(
        provider_name=name,
        base_url=endpoint,
        dataset=config["dataset"],
        zoning_code_field=fields["zoning_code"],
        zoning_description_field=fields.get("zoning_description"),
        address_field=fields.get("address"),
        geometry_field=config.get("geometry_field", "geom"),
        geocoder=geocoder,
        public_map_url=map_url,
    )


def _socrata(
    name: str,
    endpoint: str,
    config: dict[str, Any],
    map_url: str | None,
    _geocoder: GeocoderProtocol | None,
) -> SocrataZoningProvider:
    fields = config["fields"]
    return SocrataZoningProvider(
        provider_name=name,
        base_url=endpoint,
        dataset_id=config["dataset"],
        zoning_code_field=fields["zoning_code"],
        address_field=fields["address"],
        zoning_description_field=fields.get("zoning_description"),
        public_map_url=map_url,
    )
