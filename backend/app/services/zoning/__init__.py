"""Zoning lookup.

    address -> municipality provider -> zone -> sign bylaw retrieval

An adapter architecture, because every municipality publishes differently:
ArcGIS REST, Opendatasoft, Socrata, or nothing at all. Adding a city is a
provider module plus a configuration row on ``municipality`` — no core change,
and no province conditionals.

Deliberately *not* a GIS platform. No spatial arithmetic, no PostGIS, no
scraping. The only question asked is "what zone is this parcel", and the answer
always carries the city's own map link so a person can check it.
"""

from __future__ import annotations

from app.services.zoning.base import (
    ZoningLookup,
    ZoningProviderProtocol,
    ZoningResult,
    normalize_address,
)
from app.services.zoning.zoning_service import (
    ZoningOutcome,
    ZoningReport,
    ZoningService,
)

__all__ = [
    "ZoningLookup",
    "ZoningOutcome",
    "ZoningProviderProtocol",
    "ZoningReport",
    "ZoningResult",
    "ZoningService",
    "normalize_address",
]
