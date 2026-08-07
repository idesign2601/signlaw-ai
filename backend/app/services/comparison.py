"""Municipality comparison.

    "Compare Burnaby and Vancouver fascia sign requirements"

Retrieves each municipality's governing text **separately** and places the
results side by side. Deliberately not one retrieval across both: a single
ranked list mixes the two corpora, and the model then has to keep track of which
passage belongs to which city while writing. That is precisely the mistake with
the highest cost here — an answer attributing Vancouver's limit to Burnaby is
wrong in a way that reads perfectly.

Each side is retrieved, measured and cited independently. The comparison itself
is arithmetic on the two measured values, not a judgement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.services.compliance.base import (
    ComplianceOutcome,
    Dimension,
    MeasuredValue,
    SignType,
)
from app.services.compliance.engine import ComplianceEngine
from app.services.compliance.rules import locations_for

__all__ = ["ComparisonReport", "ComparisonRow", "MunicipalityComparisonService"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ComparisonSide:
    """One municipality's answer for one dimension."""

    municipality_slug: str
    value: float | None
    unit: str = ""
    is_ratio_of_frontage: bool = False
    evidence: MeasuredValue | None = None
    detail: str = ""

    @property
    def is_known(self) -> bool:
        return self.value is not None


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    """One dimension, across every municipality asked about."""

    dimension: Dimension
    sides: tuple[ComparisonSide, ...]

    @property
    def is_comparable(self) -> bool:
        """Whether the values can honestly be compared.

        Two known values in the same unit and the same form. A flat limit and a
        per-metre ratio are not comparable without a frontage, and presenting
        "9.3" beside "0.2" as though they were would be actively misleading.
        """
        known = [side for side in self.sides if side.is_known]
        if len(known) < 2:
            return False
        return (
            len({side.unit for side in known}) == 1
            and len({side.is_ratio_of_frontage for side in known}) == 1
        )

    @property
    def summary(self) -> str:
        if not self.is_comparable:
            return "Not directly comparable."

        known = sorted(
            (side for side in self.sides if side.is_known),
            key=lambda side: side.value or 0.0,
        )
        lowest, highest = known[0], known[-1]

        if lowest.value == highest.value:
            return "The same in both."
        return (
            f"{highest.municipality_slug} permits more "
            f"({highest.value:g} vs {lowest.value:g} {highest.unit})."
        )


@dataclass
class ComparisonReport:
    """A side-by-side comparison, with every figure cited."""

    sign_type: SignType
    municipality_slugs: tuple[str, ...]
    rows: list[ComparisonRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def citations(self) -> list[MeasuredValue]:
        return [
            side.evidence
            for row in self.rows
            for side in row.sides
            if side.evidence is not None
        ]


@dataclass
class MunicipalityComparisonService:
    """Compares sign rules across municipalities, one retrieval each."""

    engine: ComplianceEngine

    async def compare(
        self, sign_type: SignType, municipality_slugs: tuple[str, ...]
    ) -> ComparisonReport:
        report = ComparisonReport(
            sign_type=sign_type, municipality_slugs=municipality_slugs
        )

        if len(municipality_slugs) < 2:
            report.warnings.append("Comparison needs at least two municipalities.")
            return report

        dimensions = tuple(
            dict.fromkeys(
                location.dimension
                for location in locations_for(sign_type)
                if location.dimension in {Dimension.AREA, Dimension.HEIGHT, Dimension.SETBACK}
            )
        )

        for dimension in dimensions:
            sides = [
                await self._side(sign_type, slug, dimension)
                for slug in municipality_slugs
            ]
            report.rows.append(ComparisonRow(dimension=dimension, sides=tuple(sides)))

        if any(not row.is_comparable for row in report.rows):
            report.warnings.append(
                "Some rules could not be compared directly — they are expressed "
                "differently, or one municipality's bylaw is silent. Read the "
                "cited sections rather than inferring from the gaps."
            )

        return report

    async def _side(
        self, sign_type: SignType, municipality_slug: str, dimension: Dimension
    ) -> ComparisonSide:
        """Measure one municipality's limit for one dimension.

        Uses the compliance engine's own retrieval and parsing, with a probe
        value, so the comparison and a compliance check can never disagree about
        what a municipality's limit is.
        """
        from app.services.compliance.base import SignSpec

        # A probe magnitude large enough that the check is evaluated rather
        # than skipped. The verdict is discarded; only the limit is wanted.
        probe = SignSpec(
            sign_type=sign_type,
            municipality_slug=municipality_slug,
            area_sq_m=1.0,
            height_m=1.0,
            setback_m=1.0,
            # Supplied so a ratio rule resolves to a comparable number. Stated
            # in the output, because 0.2 m² per metre only becomes a limit once
            # a frontage is chosen.
            frontage_m=1.0,
        )

        report = await self.engine.check(probe)
        check = next(
            (item for item in report.checks if item.dimension is dimension), None
        )

        if check is None or check.outcome is ComplianceOutcome.INSUFFICIENT_INFORMATION:
            return ComparisonSide(
                municipality_slug=municipality_slug,
                value=None,
                detail=(
                    check.detail
                    if check is not None
                    else "No governing section found in the indexed bylaw."
                ),
            )

        return ComparisonSide(
            municipality_slug=municipality_slug,
            value=check.limit,
            unit=check.unit,
            is_ratio_of_frontage=bool(
                check.evidence and check.evidence.is_ratio_of_frontage
            ),
            evidence=check.evidence,
        )
