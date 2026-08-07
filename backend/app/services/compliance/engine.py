"""The compliance engine.

For each dimension of a proposed sign: retrieve the governing bylaw section,
parse the limit out of the retrieved text, compare, and return the verdict
**with the passage it was computed from**.

Where any step fails — nothing retrieved, nothing parseable, the wrong unit —
the result is ``INSUFFICIENT_INFORMATION``. Never a pass, never a fail. A sign
is not compliant because the system could not find the rule.

This is the whole design. The engine holds no regulation; it holds a procedure
for finding one and arithmetic to apply once it has.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.rag.results import RetrievedChunk
from app.rag.retriever import RetrievalFilters
from app.services.compliance.base import (
    ComplianceCheck,
    ComplianceOutcome,
    ComplianceReport,
    Dimension,
    MeasuredValue,
    RuleLocation,
    SignSpec,
)
from app.services.compliance.parsing import extract_limit
from app.services.compliance.rules import locations_for
from app.services.rag_service import RetrieverProtocol

__all__ = ["ComplianceEngine"]

logger = get_logger(__name__)

# Which spec field each dimension is compared against.
_PROPOSED: dict[Dimension, str] = {
    Dimension.AREA: "area_sq_m",
    Dimension.HEIGHT: "height_m",
    Dimension.SETBACK: "setback_m",
}

# A setback is a *minimum*; everything else here is a maximum. Getting this
# backwards would report a compliant sign as too close and vice versa.
_MINIMUMS = {Dimension.SETBACK}


@dataclass
class ComplianceEngine:
    """Checks a proposed sign against retrieved bylaw text."""

    retriever: RetrieverProtocol
    #: Below this the retrieved passage is not trusted to be the governing rule.
    #: Deliberately strict: a weak match that happens to contain a number is the
    #: one failure mode that produces a confident wrong answer.
    min_rerank_score: float = 0.0
    top_n: int = 5

    async def check(self, spec: SignSpec) -> ComplianceReport:
        report = ComplianceReport(spec=spec)

        for location in locations_for(spec.sign_type, spec.municipality_slug):
            check = await self._check_one(spec, location)
            if check is not None:
                report.checks.append(check)

        if spec.frontage_m is None and any(
            location.is_ratio_of_frontage
            for location in locations_for(spec.sign_type, spec.municipality_slug)
        ):
            report.warnings.append(
                "This municipality expresses sign area as a ratio of building "
                "frontage. Supply the frontage to evaluate the area limit."
            )

        if report.outcome is ComplianceOutcome.INSUFFICIENT_INFORMATION:
            report.warnings.append(
                "Some rules could not be established from the indexed bylaw. "
                "Confirm with the municipality before fabricating."
            )

        return report

    # -- one dimension -------------------------------------------------------

    async def _check_one(
        self, spec: SignSpec, location: RuleLocation
    ) -> ComplianceCheck | None:
        proposed = self._proposed(spec, location.dimension)

        # Permit and illumination are not numeric comparisons; they are answered
        # by the RAG pipeline as prose, not here.
        if location.dimension not in _PROPOSED:
            return None
        if proposed is None:
            return None

        chunks = await self._retrieve(spec, location)
        if not chunks:
            return ComplianceCheck(
                dimension=location.dimension,
                outcome=ComplianceOutcome.INSUFFICIENT_INFORMATION,
                proposed=proposed,
                detail=(
                    "No section of the indexed bylaw was found governing "
                    f"{location.dimension.value} for this sign type."
                ),
            )

        measured = self._measure(chunks, location)
        if measured is None:
            return ComplianceCheck(
                dimension=location.dimension,
                outcome=ComplianceOutcome.INSUFFICIENT_INFORMATION,
                proposed=proposed,
                detail=(
                    "A relevant section was found but states no numeric limit "
                    "this system can read — it may refer to a schedule or table. "
                    "Read the cited section."
                ),
                evidence=self._as_evidence(chunks[0], location, value=None),
            )

        limit = measured.limit_for(spec.frontage_m)
        if limit is None:
            return ComplianceCheck(
                dimension=location.dimension,
                outcome=ComplianceOutcome.INSUFFICIENT_INFORMATION,
                proposed=proposed,
                unit=measured.unit,
                evidence=measured,
                detail=(
                    "The limit is expressed per metre of frontage, which was "
                    "not supplied."
                ),
            )

        within = (
            proposed >= limit
            if location.dimension in _MINIMUMS
            else proposed <= limit
        )

        return ComplianceCheck(
            dimension=location.dimension,
            outcome=(
                ComplianceOutcome.COMPLIES if within else ComplianceOutcome.EXCEEDS
            ),
            proposed=proposed,
            limit=limit,
            unit=measured.unit,
            evidence=measured,
            detail=self._detail(location.dimension, proposed, limit, measured.unit, within),
        )

    async def _retrieve(
        self, spec: SignSpec, location: RuleLocation
    ) -> list[RetrievedChunk]:
        query = " ".join(
            (
                *location.search_terms[:2],
                location.dimension.value,
                spec.sign_type.value.replace("_", " "),
            )
        )

        try:
            chunks, _ = await self.retriever.retrieve(
                query,
                filters=RetrievalFilters(
                    municipality_slugs=(spec.municipality_slug,),
                    in_force_only=True,
                    # A number misread by OCR is indistinguishable from a
                    # correct one, and this output is measured and fabricated
                    # from. Excluded rather than flagged.
                    exclude_ocr=True,
                ),
                top_n=self.top_n,
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never fail the request
            logger.warning(
                "compliance_retrieval_failed",
                dimension=location.dimension.value,
                error=str(exc),
            )
            return []

        return chunks

    def _measure(
        self, chunks: list[RetrievedChunk], location: RuleLocation
    ) -> MeasuredValue | None:
        """Parse a limit from the best-matching chunk that yields one."""
        for chunk in chunks:
            # final_score is the reranker's verdict where it ran, else the
            # fusion score. Either way it is the ordering the retriever chose.
            if chunk.final_score < self.min_rerank_score:
                continue

            limit = extract_limit(chunk.body, expected_units=location.expected_units)
            if limit is None:
                continue

            return MeasuredValue(
                value=limit.value,
                unit=limit.unit,
                quote=limit.sentence,
                section=chunk.section_number,
                page=chunk.page_number,
                document_title=chunk.document_title,
                bylaw_number=chunk.bylaw_number,
                municipality=chunk.municipality_name,
                is_ratio_of_frontage=limit.is_ratio_of_frontage,
            )

        return None

    @staticmethod
    def _as_evidence(
        chunk: RetrievedChunk, location: RuleLocation, *, value: float | None
    ) -> MeasuredValue:
        """Cite the section even when no number could be read from it."""
        return MeasuredValue(
            value=value or 0.0,
            unit="",
            quote=chunk.body[:400],
            section=chunk.section_number,
            page=chunk.page_number,
            document_title=chunk.document_title,
            bylaw_number=chunk.bylaw_number,
            municipality=chunk.municipality_name,
            is_ratio_of_frontage=location.is_ratio_of_frontage,
        )

    @staticmethod
    def _proposed(spec: SignSpec, dimension: Dimension) -> float | None:
        attribute = _PROPOSED.get(dimension)
        return getattr(spec, attribute) if attribute else None

    @staticmethod
    def _detail(
        dimension: Dimension, proposed: float, limit: float, unit: str, within: bool
    ) -> str:
        comparison = "at least" if dimension in _MINIMUMS else "at most"
        verdict = "within" if within else "outside"
        return (
            f"Proposed {proposed:g} {unit}; the bylaw requires {comparison} "
            f"{limit:g} {unit}. This is {verdict} the limit."
        )
