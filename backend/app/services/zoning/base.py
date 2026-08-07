"""Zoning provider contract.

One protocol, one result type. A municipality's provider translates between its
own open data — ArcGIS REST, WFS, a CKAN dataset, a bespoke JSON endpoint — and
the shape below. Nothing outside ``providers/`` knows which of those a city uses.

Adding a municipality is a provider class plus a configuration row. No core
change, which is the whole point of the seam.

**Two properties every provider must preserve.**

*Never guess.* A provider that cannot identify the parcel returns ``None``. It
does not return the nearest match, the first result, or a default zone. A wrong
zone produces a wrong sign rule that reads exactly like a right one.

*Report your own confidence.* An exact parcel match and a fuzzy street-name
match are both "a result", and the difference has to survive to the answer.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = [
    "ZoningLookup",
    "ZoningProviderProtocol",
    "ZoningResult",
    "normalize_address",
]


_PUNCTUATION = re.compile(r"[^a-z0-9]+")

# Written long in addresses and abbreviated in open data, or the reverse.
# Normalising both directions means "123 Main St" and "123 Main Street" are one
# cache key rather than two lookups with potentially different answers.
_ABBREVIATIONS = {
    "street": "st",
    "avenue": "ave",
    "road": "rd",
    "drive": "dr",
    "boulevard": "blvd",
    "crescent": "cres",
    "place": "pl",
    "court": "crt",
    "highway": "hwy",
    "parkway": "pkwy",
    "terrace": "terr",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northwest": "nw",
    "northeast": "ne",
    "southwest": "sw",
    "southeast": "se",
}


def normalize_address(value: str) -> str:
    """Reduce an address to a stable cache key.

    Lower-cases, strips accents and punctuation, and folds the common street-type
    and direction abbreviations. Deliberately lossy: this is a key, not an
    address, and the original is stored alongside it for display.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(char for char in decomposed if not unicodedata.combining(char))
    tokens = _PUNCTUATION.sub(" ", ascii_only.casefold()).split()
    return " ".join(_ABBREVIATIONS.get(token, token) for token in tokens)


@dataclass(frozen=True, slots=True)
class ZoningLookup:
    """What a provider is asked to resolve."""

    address: str
    municipality_slug: str

    @property
    def normalized(self) -> str:
        return normalize_address(self.address)


@dataclass(frozen=True, slots=True)
class ZoningResult:
    """What a provider found, and how sure it is.

    ``source_url`` is not optional in spirit even though it is typed so: an
    automated zoning answer a user cannot check against the city's own map is
    of limited use, and every provider should populate it.
    """

    zoning_code: str
    zoning_description: str | None = None
    parcel_number: str | None = None
    legal_description: str | None = None
    address: str | None = None
    # The provider's own geometry handle — an object id, a centroid, a polygon.
    # Stored opaquely so PostGIS can consume it later without a reshape.
    geometry_reference: dict[str, object] | None = None
    source_url: str | None = None
    provider: str = ""
    # 1.0 for an exact parcel match; lower for anything inferred.
    confidence: float = 0.0
    # How long this stays trustworthy. Zoning changes by rezoning application,
    # which is infrequent but not rare — a month is a reasonable default, and a
    # provider that knows better should say so.
    ttl: timedelta = timedelta(days=30)

    @property
    def expires_at(self) -> datetime:
        return datetime.now(UTC) + self.ttl

    @property
    def is_confident(self) -> bool:
        """Whether this is solid enough to drive a sign-rule answer.

        Below this, the honest response is to show the zone as unverified and
        link the user to the city's map rather than proceeding to compute
        setbacks from it.
        """
        return self.confidence >= 0.8


@runtime_checkable
class ZoningProviderProtocol(Protocol):
    """What every municipality's zoning adapter implements."""

    @property
    def name(self) -> str:
        """Stable identifier, recorded on cached rows."""
        ...

    @property
    def map_url(self) -> str | None:
        """The city's public map, for a human to verify against."""
        ...

    async def lookup(self, request: ZoningLookup) -> ZoningResult | None:
        """Resolve an address to a zone, or ``None`` when it cannot.

        Must not raise for an expected failure — an unreachable endpoint, an
        address with no match, a malformed response — because a zoning lookup
        failing should degrade the answer, not fail the request.
        """
        ...
