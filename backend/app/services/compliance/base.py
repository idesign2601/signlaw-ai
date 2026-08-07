"""Compliance types, and the rule that governs the whole subsystem.

**No municipal regulation is written in this codebase.**

That is the constraint everything here exists to enforce. Encoding "maximum
fascia sign area = 0.2 x storefront width" as Python would create a second
source of truth beside the bylaw PDF. It would drift the first time a
municipality amends its bylaw, and it would emit a confident number with no
citation — for the output someone actually fabricates a sign from.

So a :class:`RuleLocation` records only *where a rule lives*: which sign type,
which dimension, and the search terms that find the governing section. The
number itself is parsed out of retrieved bylaw text at question time, and every
computed value carries the citation it was computed from.

The consequence to keep: when a bylaw changes, a stale rule location produces a
**visible** failure — "could not find the governing section" — rather than a
quietly wrong answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "ComplianceCheck",
    "ComplianceOutcome",
    "ComplianceReport",
    "Dimension",
    "MeasuredValue",
    "RuleLocation",
    "SignSpec",
    "SignType",
]


class SignType(StrEnum):
    """Sign types municipalities regulate separately.

    Names follow common BC bylaw usage. A municipality that calls a fascia sign
    a "wall sign" is handled by the rule location's search terms, not by adding
    a member here.
    """

    FASCIA = "fascia"
    CHANNEL_LETTER = "channel_letter"
    PYLON = "pylon"
    FREESTANDING = "freestanding"
    WINDOW = "window"
    DIGITAL = "digital"
    AWNING = "awning"
    PROJECTING = "projecting"
    CANOPY = "canopy"


class Dimension(StrEnum):
    """What a rule constrains."""

    AREA = "area"
    HEIGHT = "height"
    SETBACK = "setback"
    ILLUMINATION = "illumination"
    QUANTITY = "quantity"
    PERMIT = "permit"


class ComplianceOutcome(StrEnum):
    """The verdict for one check."""

    COMPLIES = "complies"
    EXCEEDS = "exceeds"
    #: A rule was found but its numeric limit could not be parsed, or no rule
    #: was retrievable at all. Never a pass, never a fail.
    INSUFFICIENT_INFORMATION = "insufficient_information"

    @property
    def is_determinate(self) -> bool:
        return self is not ComplianceOutcome.INSUFFICIENT_INFORMATION


@dataclass(frozen=True, slots=True)
class SignSpec:
    """The proposed sign.

    Units are metric throughout — BC and Alberta bylaws are written in metres
    and square metres. Conversion belongs at the interface boundary, not here,
    so that a number in this dataclass always means the same thing.
    """

    sign_type: SignType
    municipality_slug: str
    area_sq_m: float | None = None
    height_m: float | None = None
    width_m: float | None = None
    setback_m: float | None = None
    illuminated: bool | None = None
    # Frontage of the building or unit the sign is attached to. Most fascia
    # area rules are expressed as a ratio of it.
    frontage_m: float | None = None
    building_type: str | None = None
    zoning_code: str | None = None


@dataclass(frozen=True, slots=True)
class RuleLocation:
    """Where a rule lives — never what it says.

    ``search_terms`` are what retrieval is asked for. ``expected_units`` is used
    to reject a parsed number in the wrong unit rather than silently comparing
    metres to square metres.
    """

    sign_type: SignType
    dimension: Dimension
    search_terms: tuple[str, ...]
    expected_units: tuple[str, ...] = ()
    # A rule expressed as a ratio of frontage rather than a flat limit, e.g.
    # "0.2 square metres per metre of frontage".
    is_ratio_of_frontage: bool = False
    notes: str = ""


@dataclass(frozen=True, slots=True)
class MeasuredValue:
    """A number parsed out of bylaw text, with where it came from."""

    value: float
    unit: str
    #: The sentence the number was parsed from, quoted verbatim.
    quote: str
    section: str | None = None
    page: int | None = None
    document_title: str | None = None
    bylaw_number: str | None = None
    municipality: str | None = None
    is_ratio_of_frontage: bool = False

    def limit_for(self, frontage_m: float | None) -> float | None:
        """The effective limit, applying frontage where the rule is a ratio."""
        if not self.is_ratio_of_frontage:
            return self.value
        if frontage_m is None:
            return None
        return self.value * frontage_m


@dataclass(frozen=True, slots=True)
class ComplianceCheck:
    """One dimension, checked."""

    dimension: Dimension
    outcome: ComplianceOutcome
    proposed: float | None = None
    limit: float | None = None
    unit: str = ""
    #: Always populated when the outcome is determinate. A verdict without the
    #: text it was derived from is exactly what this design forbids.
    evidence: MeasuredValue | None = None
    detail: str = ""

    @property
    def margin(self) -> float | None:
        """How much room is left, negative when over."""
        if self.proposed is None or self.limit is None:
            return None
        return self.limit - self.proposed


@dataclass
class ComplianceReport:
    """Every check for one proposed sign."""

    spec: SignSpec
    checks: list[ComplianceCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def outcome(self) -> ComplianceOutcome:
        """The report's overall verdict.

        Any determinate exceedance fails the whole sign. Otherwise, any
        indeterminate check makes the whole report indeterminate — a sign is not
        "compliant" because the checks that could be evaluated passed.
        """
        if any(check.outcome is ComplianceOutcome.EXCEEDS for check in self.checks):
            return ComplianceOutcome.EXCEEDS
        if not self.checks or any(
            not check.outcome.is_determinate for check in self.checks
        ):
            return ComplianceOutcome.INSUFFICIENT_INFORMATION
        return ComplianceOutcome.COMPLIES

    @property
    def citations(self) -> list[MeasuredValue]:
        """Every piece of bylaw text this report rests on."""
        return [check.evidence for check in self.checks if check.evidence is not None]
