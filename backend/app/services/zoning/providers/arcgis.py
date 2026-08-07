"""Shared ArcGIS REST client.

Most BC and Alberta municipalities publish parcel and zoning layers through
ArcGIS REST, so the query mechanics are written once here and each city's
provider supplies only its endpoint and its field names. A new ArcGIS city is
then a subclass with four attributes rather than another HTTP client.

Field names are the whole variability: one city calls the zone ``ZONE_CODE``,
another ``Zoning``, another ``ZONE_DESC``. That is data, not logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.logging import get_logger
from app.services.zoning.base import ZoningLookup, ZoningResult

__all__ = ["ArcGisFieldMap", "ArcGisZoningProvider"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ArcGisFieldMap:
    """Which attribute of the layer holds what."""

    zoning_code: str
    address: str
    zoning_description: str | None = None
    parcel_number: str | None = None
    legal_description: str | None = None


@dataclass
class ArcGisZoningProvider:
    """Queries an ArcGIS FeatureServer or MapServer layer by address.

    Address matching uses a case-insensitive exact comparison on the layer's
    address field. Deliberately not ``LIKE '%…%'``: a substring match on "12
    Main St" also matches "112 Main St", and returning the wrong parcel is far
    worse than returning nothing.
    """

    provider_name: str
    endpoint: str
    fields: ArcGisFieldMap
    public_map_url: str | None = None
    timeout_s: float = 10.0
    extra_params: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def map_url(self) -> str | None:
        return self.public_map_url

    async def lookup(self, request: ZoningLookup) -> ZoningResult | None:
        params: dict[str, str] = {
            "f": "json",
            "outFields": "*",
            "returnGeometry": "true",
            "where": self._where(request.address),
            **self.extra_params,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.get(f"{self.endpoint}/query", params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # A zoning lookup failing must degrade the answer, never fail the
            # request: the sign bylaw question is still answerable without it.
            logger.warning(
                "zoning_provider_unavailable",
                provider=self.provider_name,
                error=str(exc),
            )
            return None

        features = payload.get("features") or []
        if not features:
            logger.info(
                "zoning_no_match", provider=self.provider_name, address=request.address
            )
            return None

        if len(features) > 1:
            # Several parcels claiming one address usually means a strata or a
            # multi-address lot. Picking one silently would attach sign rules to
            # an arbitrary parcel.
            logger.info(
                "zoning_ambiguous",
                provider=self.provider_name,
                address=request.address,
                matches=len(features),
            )
            return None

        return self._to_result(features[0])

    def _where(self, address: str) -> str:
        # Single quotes are the only SQL metacharacter that matters in an
        # ArcGIS where clause; doubling them is the standard escape.
        escaped = address.replace("'", "''").upper()
        return f"UPPER({self.fields.address}) = '{escaped}'"

    def _to_result(self, feature: dict[str, Any]) -> ZoningResult | None:
        attributes = feature.get("attributes") or {}

        code = _clean(attributes.get(self.fields.zoning_code))
        if not code:
            # A matched parcel with no zone is not a zoning result. Returning
            # it with an empty code would read downstream as "unzoned".
            return None

        return ZoningResult(
            zoning_code=code,
            zoning_description=_optional(attributes, self.fields.zoning_description),
            parcel_number=_optional(attributes, self.fields.parcel_number),
            legal_description=_optional(attributes, self.fields.legal_description),
            address=_clean(attributes.get(self.fields.address)),
            geometry_reference=feature.get("geometry"),
            source_url=self.public_map_url,
            provider=self.provider_name,
            # An exact match on the city's own address field is as good as this
            # gets without spatial work.
            confidence=1.0,
        )


def _optional(attributes: dict[str, Any], key: str | None) -> str | None:
    return _clean(attributes.get(key)) if key else None


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
