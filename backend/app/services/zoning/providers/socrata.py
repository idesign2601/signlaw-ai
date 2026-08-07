"""Shared Socrata client.

Calgary and several other Alberta municipalities publish through Socrata. Field
names and dataset identifiers come from configuration; nothing here is specific
to a city.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.logging import get_logger
from app.services.zoning.base import ZoningLookup, ZoningResult

__all__ = ["SocrataZoningProvider"]

logger = get_logger(__name__)


@dataclass
class SocrataZoningProvider:
    """Queries a Socrata dataset for the parcel at an address."""

    provider_name: str
    base_url: str
    dataset_id: str
    zoning_code_field: str
    address_field: str
    zoning_description_field: str | None = None
    public_map_url: str | None = None
    timeout_s: float = 10.0

    @property
    def name(self) -> str:
        return self.provider_name

    @property
    def map_url(self) -> str | None:
        return self.public_map_url

    async def lookup(self, request: ZoningLookup) -> ZoningResult | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.get(
                    f"{self.base_url}/resource/{self.dataset_id}.json",
                    # Two requested so an ambiguous address is detectable. One
                    # would silently return the first of several parcels.
                    params={self.address_field: request.address.upper(), "$limit": "2"},
                )
                response.raise_for_status()
                records = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "zoning_provider_unavailable",
                provider=self.provider_name,
                error=str(exc),
            )
            return None

        if not isinstance(records, list) or len(records) != 1:
            # Nothing, or several parcels claiming one address — usually a
            # strata or a multi-address lot. Choosing between them silently
            # would attach sign rules to an arbitrary property.
            return None

        record = records[0]
        code = str(record.get(self.zoning_code_field, "")).strip()
        if not code:
            return None

        description = None
        if self.zoning_description_field:
            description = (
                str(record.get(self.zoning_description_field, "")).strip() or None
            )

        return ZoningResult(
            zoning_code=code,
            zoning_description=description,
            address=str(record.get(self.address_field, "")).strip() or None,
            source_url=self.public_map_url,
            provider=self.provider_name,
            confidence=1.0,
        )
