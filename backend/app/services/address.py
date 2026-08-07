"""Address parsing and municipality detection.

    "123 Main Street Vancouver" -> municipality + street address

Splits an address into the part a zoning provider needs and the municipality
whose provider to ask. Deliberately small: this is not a geocoder and does not
validate that the address exists — the municipality's own parcel data does that,
authoritatively, one step later.

**The ambiguity rule carries over unchanged.** "123 Main Street, Langley"
resolves to nothing and asks which Langley, because the City and the Township
have separate zoning bylaws *and* separate sign bylaws. A geocoder would happily
pick one. This will not.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.domain.municipalities import MunicipalityRecord, MunicipalityRegistry
from app.domain.provinces import PROVINCES

__all__ = ["AddressOutcome", "AddressParser", "ParsedAddress"]


# A street address starts with a civic number. Without one there is no parcel to
# look up — "Main Street, Burnaby" is a street, not an address.
_CIVIC_NUMBER = re.compile(r"^\s*(\d+[a-z]?(?:\s*-\s*\d+[a-z]?)?)\s+", re.IGNORECASE)

_POSTAL_CODE = re.compile(
    r"\b[a-z]\d[a-z]\s*\d[a-z]\d\b", re.IGNORECASE
)

_PROVINCE_TOKENS = re.compile(
    r",?\s*\b(?:bc|b\.c\.|british columbia|ab|alta|alberta|canada)\b\.?\s*$",
    re.IGNORECASE,
)


class AddressOutcome(StrEnum):
    """How address parsing resolved."""

    RESOLVED = "resolved"
    AMBIGUOUS_MUNICIPALITY = "ambiguous_municipality"
    NO_MUNICIPALITY = "no_municipality"
    NO_CIVIC_NUMBER = "no_civic_number"

    @property
    def is_usable(self) -> bool:
        return self is AddressOutcome.RESOLVED


@dataclass(frozen=True, slots=True)
class ParsedAddress:
    """The result of parsing one address string."""

    outcome: AddressOutcome
    raw: str
    street_address: str = ""
    municipality: MunicipalityRecord | None = None
    province_code: str | None = None
    # Populated when the municipality name was ambiguous. Official names, so a
    # person can pick between "City of Langley" and "Township of Langley".
    candidates: tuple[str, ...] = ()
    detail: str = ""


@dataclass
class AddressParser:
    """Extracts a municipality and street address from free text."""

    registry: MunicipalityRegistry = field(default_factory=MunicipalityRegistry)

    def parse(self, value: str) -> ParsedAddress:
        raw = value.strip()
        if not raw:
            return ParsedAddress(
                outcome=AddressOutcome.NO_CIVIC_NUMBER,
                raw=raw,
                detail="Enter a street address, for example 123 Main Street, Burnaby.",
            )

        cleaned = _POSTAL_CODE.sub("", raw)
        cleaned = _PROVINCE_TOKENS.sub("", cleaned).strip().strip(",")

        found = self.registry.find_in_text(cleaned)
        if len(found) == 1:
            return self._resolved(raw, cleaned, found[0])

        if len(found) > 1:
            # Two municipality names in one address usually means a street named
            # after a city — "123 Burnaby Street, Vancouver". The trailing one
            # wins, since Canadian address order puts the municipality last.
            return self._resolved(raw, cleaned, _trailing(cleaned, found))

        ambiguous = self._ambiguous(cleaned)
        if ambiguous:
            return ParsedAddress(
                outcome=AddressOutcome.AMBIGUOUS_MUNICIPALITY,
                raw=raw,
                candidates=ambiguous,
                detail=(
                    "That municipality name matches more than one jurisdiction, "
                    "and they have separate bylaws. Which did you mean?"
                ),
            )

        return ParsedAddress(
            outcome=AddressOutcome.NO_MUNICIPALITY,
            raw=raw,
            detail=(
                "No municipality recognised in that address. Include the city, "
                "for example 123 Main Street, Burnaby."
            ),
        )

    def _resolved(
        self, raw: str, cleaned: str, record: MunicipalityRecord
    ) -> ParsedAddress:
        street = self._strip_municipality(cleaned, record)

        if not _CIVIC_NUMBER.match(street):
            # A zoning lookup needs a parcel, and a parcel needs a civic number.
            # Failing here is far better than sending a street name to a
            # provider and accepting whichever parcel comes back first.
            return ParsedAddress(
                outcome=AddressOutcome.NO_CIVIC_NUMBER,
                raw=raw,
                municipality=record,
                province_code=_province_of(record),
                detail=(
                    "That looks like a street rather than an address. Include "
                    "the building number."
                ),
            )

        return ParsedAddress(
            outcome=AddressOutcome.RESOLVED,
            raw=raw,
            street_address=street,
            municipality=record,
            province_code=_province_of(record),
        )

    @staticmethod
    def _strip_municipality(value: str, record: MunicipalityRecord) -> str:
        """Remove the municipality name, leaving the street address.

        Only a trailing occurrence is removed. "123 Burnaby Street, Vancouver"
        must keep its street name.
        """
        candidates = [record.official_name, record.name, *record.aliases]
        for candidate in sorted(candidates, key=len, reverse=True):
            pattern = re.compile(
                rf",?\s*{re.escape(candidate)}\s*,?\s*$", re.IGNORECASE
            )
            stripped = pattern.sub("", value).strip().strip(",")
            if stripped != value:
                return stripped.strip()
        return value.strip().strip(",")

    def _ambiguous(self, value: str) -> tuple[str, ...]:
        """Official names a trailing ambiguous municipality could refer to."""
        tokens = [token for token in re.split(r"[,\s]+", value) if token]

        # Longest trailing phrase first: "north vancouver" before "vancouver".
        for size in (3, 2, 1):
            if len(tokens) < size:
                continue
            phrase = " ".join(tokens[-size:])
            if self.registry.is_ambiguous(phrase):
                return tuple(
                    record.official_name
                    for record in self.registry.candidates(phrase)
                )
        return ()


def _trailing(
    value: str, records: Sequence[MunicipalityRecord]
) -> MunicipalityRecord:
    """The municipality named last in the string.

    ``find_in_text`` returns matches ordered by *name length*, longest first, so
    that "North Vancouver" is not mistaken for "Vancouver". That ordering says
    nothing about where in the string each name appeared — taking its last
    element resolved "123 Burnaby Street, Vancouver" to Burnaby.

    Position is what matters here, because Canadian address order puts the
    municipality after the street.
    """
    lowered = value.casefold()

    def position(record: MunicipalityRecord) -> int:
        names = (record.official_name, record.name, *record.aliases)
        return max((lowered.rfind(name.casefold()) for name in names), default=-1)

    return max(records, key=position)


def _province_of(record: MunicipalityRecord) -> str | None:
    for province in PROVINCES:
        if any(item.slug == record.slug for item in province.municipalities):
            return province.code
    return None
