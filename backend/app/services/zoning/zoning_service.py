"""Zoning lookup orchestration.

Resolves an address to a zone by asking the municipality's own provider, caching
the answer, and — this is the part that matters — being explicit about what it
does not know.

Four outcomes, and the interface must distinguish them:

``resolved``
    A zone was found and is current enough to rely on.

``stale``
    A cached zone exists but has expired and the provider is unreachable. Shown
    with its as-at date, because a month-old zone is usually right and
    occasionally not.

``unsupported``
    The municipality has no zoning provider configured. Not a failure — most
    municipalities do not publish queryable parcel data — and saying so is more
    useful than an empty result.

``not_found``
    The provider ran and found nothing. Genuinely different from the above: it
    means the address is wrong, or outside the municipality.

Never a fifth outcome where a plausible zone is invented.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.zoning.base import ZoningLookup, ZoningResult, normalize_address
from app.services.zoning.providers import build_provider

__all__ = ["ZoningOutcome", "ZoningReport", "ZoningService"]

logger = get_logger(__name__)


class ZoningOutcome(StrEnum):
    """How a zoning lookup resolved."""

    RESOLVED = "resolved"
    STALE = "stale"
    UNSUPPORTED = "unsupported"
    NOT_FOUND = "not_found"

    @property
    def is_usable(self) -> bool:
        """Whether a sign rule may be derived from this.

        Stale counts, with its date shown. Anything else does not: computing
        setbacks from a zone we could not establish is precisely the confident
        wrong answer this product exists to avoid.
        """
        return self in {ZoningOutcome.RESOLVED, ZoningOutcome.STALE}


@dataclass(frozen=True, slots=True)
class _GisConfig:
    """A municipality's zoning service configuration, as stored.

    ``verified`` gates whether the provider is built at all. An unverified
    endpoint is never queried: a layer carrying a similar-looking field returns
    a confidently wrong zone, and the service responds happily either way.
    """

    municipality_id: uuid.UUID
    kind: str | None
    endpoint: str | None
    settings: dict[str, object]
    verified: bool
    map_url: str | None


@dataclass(frozen=True, slots=True)
class ZoningReport:
    """A zoning answer, with its provenance."""

    outcome: ZoningOutcome
    municipality_slug: str
    address: str
    zoning_code: str | None = None
    zoning_description: str | None = None
    parcel_number: str | None = None
    legal_description: str | None = None
    source_url: str | None = None
    map_url: str | None = None
    provider: str | None = None
    confidence: float = 0.0
    # When the underlying data was fetched. Displayed with the answer: zoning
    # changes by rezoning application, and "as at" is part of the claim.
    as_of: datetime | None = None
    detail: str = ""


@dataclass
class ZoningService:
    """Looks up a parcel's zone, with caching and honest failure."""

    session: AsyncSession

    async def lookup(self, address: str, municipality_slug: str) -> ZoningReport:
        config = await self._municipality(municipality_slug)
        if config is None:
            return ZoningReport(
                outcome=ZoningOutcome.UNSUPPORTED,
                municipality_slug=municipality_slug,
                address=address,
                detail="That municipality is not indexed.",
            )

        cached = await self._cached(config.municipality_id, address)
        if cached is not None and not _expired(cached.expires_at):
            return _report_from_cache(cached, municipality_slug, address, config.map_url)

        provider = (
            build_provider(
                config.kind,
                endpoint=config.endpoint,
                config=config.settings,
                map_url=config.map_url,
                name=municipality_slug,
            )
            if config.verified
            else None
        )
        if provider is None:
            # No provider, but possibly an expired cache. An old zone with its
            # date beats no answer, so long as the date is shown.
            if cached is not None:
                return _report_from_cache(
                    cached, municipality_slug, address, config.map_url, stale=True
                )
            return ZoningReport(
                outcome=ZoningOutcome.UNSUPPORTED,
                municipality_slug=municipality_slug,
                address=address,
                map_url=config.map_url,
                detail=(
                    "Zoning lookup is not configured for this municipality. "
                    "Check the city's own map."
                    if not config.verified
                    else "This municipality does not publish queryable zoning data."
                ),
            )

        result = await provider.lookup(
            ZoningLookup(address=address, municipality_slug=municipality_slug)
        )

        if result is None:
            if cached is not None:
                return _report_from_cache(
                    cached, municipality_slug, address, config.map_url, stale=True
                )
            return ZoningReport(
                outcome=ZoningOutcome.NOT_FOUND,
                municipality_slug=municipality_slug,
                address=address,
                map_url=config.map_url,
                provider=provider.name,
                detail=(
                    "No parcel matched that address. Check the spelling, or "
                    "that the property is inside this municipality."
                ),
            )

        await self._cache(config.municipality_id, address, result)

        return ZoningReport(
            outcome=ZoningOutcome.RESOLVED,
            municipality_slug=municipality_slug,
            address=address,
            zoning_code=result.zoning_code,
            zoning_description=result.zoning_description,
            parcel_number=result.parcel_number,
            legal_description=result.legal_description,
            source_url=result.source_url,
            map_url=config.map_url or provider.map_url,
            provider=result.provider or provider.name,
            confidence=result.confidence,
            as_of=datetime.now(UTC),
        )

    # -- persistence ---------------------------------------------------------

    async def _municipality(self, slug: str) -> _GisConfig | None:
        row = (
            await self.session.execute(
                text(
                    "SELECT id, gis_provider, gis_endpoint, gis_config, "
                    "       gis_verified, map_url "
                    "FROM municipality WHERE canonical_slug = :slug"
                ),
                {"slug": slug},
            )
        ).first()

        if row is None:
            return None

        return _GisConfig(
            municipality_id=row.id,
            kind=row.gis_provider,
            endpoint=row.gis_endpoint,
            settings=dict(row.gis_config or {}),
            verified=bool(row.gis_verified),
            map_url=row.map_url,
        )

    async def _cached(
        self, municipality_id: uuid.UUID, address: str
    ) -> Row[Any] | None:
        return (
            await self.session.execute(
                text(
                    "SELECT zoning_code, zoning_description, parcel_number, "
                    "       legal_description, source_url, provider, confidence, "
                    "       fetched_at, expires_at "
                    "FROM parcel_zoning "
                    "WHERE municipality_id = :municipality_id "
                    "  AND normalized_address = :address"
                ),
                {
                    "municipality_id": municipality_id,
                    "address": normalize_address(address),
                },
            )
        ).first()

    async def _cache(
        self, municipality_id: uuid.UUID, address: str, result: ZoningResult
    ) -> None:
        await self.session.execute(
            text(
                "INSERT INTO parcel_zoning (id, municipality_id, address, "
                " normalized_address, parcel_number, legal_description, "
                " zoning_code, zoning_description, geometry_reference, source_url, "
                " provider, confidence, fetched_at, expires_at) "
                "VALUES (:id, :municipality_id, :address, :normalized, :parcel, "
                " :legal, :code, :description, CAST(:geometry AS jsonb), :source, "
                " :provider, :confidence, :fetched, :expires) "
                "ON CONFLICT ON CONSTRAINT uq_parcel_zoning_municipality_address "
                "DO UPDATE SET "
                " parcel_number = EXCLUDED.parcel_number, "
                " legal_description = EXCLUDED.legal_description, "
                " zoning_code = EXCLUDED.zoning_code, "
                " zoning_description = EXCLUDED.zoning_description, "
                " geometry_reference = EXCLUDED.geometry_reference, "
                " source_url = EXCLUDED.source_url, "
                " provider = EXCLUDED.provider, "
                " confidence = EXCLUDED.confidence, "
                " fetched_at = EXCLUDED.fetched_at, "
                " expires_at = EXCLUDED.expires_at, "
                " updated_at = now()"
            ),
            {
                "id": uuid.uuid4(),
                "municipality_id": municipality_id,
                "address": address,
                "normalized": normalize_address(address),
                "parcel": result.parcel_number,
                "legal": result.legal_description,
                "code": result.zoning_code,
                "description": result.zoning_description,
                "geometry": _json(result.geometry_reference),
                "source": result.source_url,
                "provider": result.provider,
                "confidence": result.confidence,
                "fetched": datetime.now(UTC),
                "expires": result.expires_at,
            },
        )
        await self.session.commit()


def _expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at <= datetime.now(UTC)


def _report_from_cache(
    row: Row[Any],
    municipality_slug: str,
    address: str,
    map_url: str | None,
    *,
    stale: bool = False,
) -> ZoningReport:
    return ZoningReport(
        outcome=ZoningOutcome.STALE if stale else ZoningOutcome.RESOLVED,
        municipality_slug=municipality_slug,
        address=address,
        zoning_code=row.zoning_code,
        zoning_description=row.zoning_description,
        parcel_number=row.parcel_number,
        legal_description=row.legal_description,
        source_url=row.source_url,
        map_url=map_url,
        provider=row.provider,
        confidence=row.confidence,
        as_of=row.fetched_at,
        detail=(
            "The city's data could not be reached, so this is the last known "
            "zoning. Confirm against the city's map before relying on it."
            if stale
            else ""
        ),
    )


def _json(value: object) -> str | None:
    if value is None:
        return None
    import json

    return json.dumps(value, default=str)
