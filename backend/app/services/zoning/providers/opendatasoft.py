"""Shared Opendatasoft client.

Vancouver and several other BC municipalities publish through Opendatasoft
rather than ArcGIS. The query grammar differs; the contract does not.

Opendatasoft zoning datasets are usually *polygons* with no address field, so a
direct address query returns nothing. Resolving an address therefore needs two
steps — address to a point, point to the containing polygon — and the first step
needs a geocoder. Where a municipality also publishes an address-bearing parcel
dataset, that is queried directly and the geocoder is skipped.

A provider configured with a polygon dataset and no geocoder reports itself
unavailable rather than returning the first polygon in the city.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.logging import get_logger
from app.services.zoning.base import ZoningLookup, ZoningResult

__all__ = ["GeocoderProtocol", "OpendatasoftZoningProvider"]

logger = get_logger(__name__)


class GeocoderProtocol(Protocol):
    """Turns an address into a point. Supplied per municipality."""

    async def locate(self, address: str) -> tuple[float, float] | None:
        """Return ``(latitude, longitude)``, or ``None`` when unresolvable."""
        ...


@dataclass
class OpendatasoftZoningProvider:
    """Queries an Opendatasoft dataset for the zone covering an address."""

    provider_name: str
    base_url: str
    dataset: str
    zoning_code_field: str
    zoning_description_field: str | None = None
    # When the dataset carries addresses, query it directly. When it is
    # polygons, a geocoder is required to turn the address into a point.
    address_field: str | None = None
    geometry_field: str = "geom"
    geocoder: GeocoderProtocol | None = None
    public_map_url: str | None = None
    timeout_s: float = 10.0

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def map_url(self) -> str | None:
        return self.public_map_url

    @property
    def is_configured(self) -> bool:
        """Whether this provider can actually resolve an address.

        A polygon dataset with no geocoder cannot. Saying so is the point: the
        alternative is a lookup that appears to work and returns an arbitrary
        polygon.
        """
        return bool(self.address_field) or self.geocoder is not None

    async def lookup(self, request: ZoningLookup) -> ZoningResult | None:
        if not self.is_configured:
            logger.info(
                "zoning_provider_unconfigured",
                provider=self.provider_name,
                detail="polygon dataset with no geocoder; cannot resolve an address",
            )
            return None

        params = await self._params(request)
        if params is None:
            return None

        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.get(
                    f"{self.base_url}/api/records/1.0/search/", params=params
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "zoning_provider_unavailable",
                provider=self.provider_name,
                error=str(exc),
            )
            return None

        records = payload.get("records") or []
        if not records:
            return None

        return self._to_result(records[0], geocoded=self.address_field is None)

    async def _params(self, request: ZoningLookup) -> dict[str, str] | None:
        base = {"dataset": self.dataset, "rows": "1"}

        if self.address_field:
            return {**base, "q": f'{self.address_field}:"{request.address}"'}

        assert self.geocoder is not None
        point = await self.geocoder.locate(request.address)
        if point is None:
            logger.info("zoning_geocode_failed", address=request.address)
            return None

        latitude, longitude = point
        # 1 metre: the point is inside the parcel or it is not. A wider radius
        # would silently return a neighbouring lot's zone.
        return {**base, "geofilter.distance": f"{latitude},{longitude},1"}

    def _to_result(self, record: dict[str, Any], *, geocoded: bool) -> ZoningResult | None:
        fields = record.get("fields") or {}

        code = _clean(fields.get(self.zoning_code_field))
        if not code:
            return None

        return ZoningResult(
            zoning_code=code,
            zoning_description=(
                _clean(fields.get(self.zoning_description_field))
                if self.zoning_description_field
                else None
            ),
            geometry_reference=record.get("geometry"),
            source_url=self.public_map_url,
            provider=self.provider_name,
            # A geocoded point carries the geocoder's error as well as the
            # dataset's. An address matched in the dataset itself does not.
            confidence=0.85 if geocoded else 1.0,
        )


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
