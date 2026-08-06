"""Confidence scoring.

A composite of independent signals, never the model's own estimate of itself.
Language models are poorly calibrated about their own reliability and report
high confidence for fluent fabrication — which is precisely the failure this
score exists to catch.

Signals, and why each is here:

* **Citation coverage** — how much of the answer is backed by verified quotes.
  The strongest signal available, because it is measured against the evidence
  rather than inferred.
* **Retrieval margin** — the gap between the best result and the rest. A flat
  distribution means nothing matched particularly well.
* **Corroboration** — whether several independent chunks agree. One chunk
  saying something might be a fragment taken out of context; three sections
  agreeing rarely is.
* **Currency** — whether the cited documents are in force. Citing superseded
  text is the failure mode most likely to cause real harm.
* **Source quality** — OCR'd pages have no reliable section numbering, so a
  citation resting on one is weaker even when the text looks clean.
* **Specificity** — whether the question named a municipality at all, and
  whether the answer resolved to a specific section.

Bands, not bare numbers: ``0.73`` means nothing to a reader, "Medium — the
bylaw is current but only one section was found" does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.db.enums import ConfidenceBand, DocumentStatus
from app.rag.results import RetrievedChunk

__all__ = ["ConfidenceFactor", "ConfidenceReport", "ConfidenceScorer"]


@dataclass(frozen=True, slots=True)
class ConfidenceFactor:
    """One contributing signal, with its reasoning preserved."""

    name: str
    score: float
    weight: float
    detail: str

    @property
    def contribution(self) -> float:
        return self.score * self.weight


@dataclass(frozen=True, slots=True)
class ConfidenceReport:
    """The score, its band, and why."""

    score: float
    band: ConfidenceBand
    factors: tuple[ConfidenceFactor, ...]
    explanation: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "score": round(self.score, 3),
            "band": self.band.value,
            "explanation": self.explanation,
            "warnings": list(self.warnings),
            "factors": {
                factor.name: {
                    "score": round(factor.score, 3),
                    "weight": factor.weight,
                    "detail": factor.detail,
                }
                for factor in self.factors
            },
        }


@dataclass
class ConfidenceScorer:
    """Scores an answer from its evidence.

    Weights are deliberately front-loaded onto citation coverage and currency.
    Both are cheap to measure and directly predict the failures that matter: an
    uncited claim and a repealed citation.
    """

    weight_citation: float = 0.30
    weight_currency: float = 0.20
    # Corroboration outweighs margin deliberately. Several sections agreeing is
    # stronger evidence than one result scoring far above the rest, and when
    # margin was the heavier of the two, adding a corroborating section could
    # *lower* total confidence — an inversion that made the score meaningless.
    weight_corroboration: float = 0.20
    weight_margin: float = 0.10
    weight_source_quality: float = 0.10
    weight_specificity: float = 0.10

    high_threshold: float = 0.75
    medium_threshold: float = 0.50
    insufficient_threshold: float = 0.25

    def score(
        self,
        *,
        chunks: Sequence[RetrievedChunk],
        cited_chunk_ids: Sequence[str],
        citation_precision: float,
        uncited_claim_count: int,
        municipality_resolved: bool,
        has_conflicts: bool = False,
        abstained: bool = False,
    ) -> ConfidenceReport:
        """Score an answer."""
        if abstained or not chunks:
            return ConfidenceReport(
                score=0.0,
                band=ConfidenceBand.INSUFFICIENT,
                factors=(),
                explanation=("No answer was produced: the indexed bylaws do not support one."),
                warnings=("no supporting bylaw text was found",),
            )

        cited = [chunk for chunk in chunks if chunk.chunk_id in set(cited_chunk_ids)]
        warnings: list[str] = []

        factors = (
            self._citation_factor(citation_precision, uncited_claim_count, cited, warnings),
            self._currency_factor(cited or list(chunks), warnings),
            self._corroboration_factor(cited, has_conflicts, warnings),
            self._margin_factor(chunks, warnings),
            self._source_quality_factor(cited or list(chunks), warnings),
            self._specificity_factor(municipality_resolved, cited, warnings),
        )

        total_weight = sum(factor.weight for factor in factors)
        raw = sum(factor.contribution for factor in factors) / max(total_weight, 1e-9)
        score = round(min(1.0, max(0.0, raw)), 3)

        band = self._band_for(score)
        # A repealed or superseded citation caps confidence regardless of how
        # well everything else scored. The text may be perfectly matched and
        # still not be the law.
        if any(chunk.document_status is not DocumentStatus.IN_FORCE for chunk in cited):
            band = min(band, ConfidenceBand.LOW, key=_band_rank)

        return ConfidenceReport(
            score=score,
            band=band,
            factors=factors,
            explanation=self._explain(band, factors),
            warnings=tuple(warnings),
        )

    # -- factors -------------------------------------------------------------

    def _citation_factor(
        self,
        precision: float,
        uncited_count: int,
        cited: Sequence[RetrievedChunk],
        warnings: list[str],
    ) -> ConfidenceFactor:
        if not cited:
            warnings.append("no verified citation supports this answer")
            return ConfidenceFactor("citation", 0.0, self.weight_citation, "no verified citations")

        score = precision
        if uncited_count:
            # Each uncited assertion is a claim the reader cannot check.
            score *= max(0.3, 1.0 - 0.25 * uncited_count)
            warnings.append(f"{uncited_count} statement(s) assert requirements without a citation")

        return ConfidenceFactor(
            "citation",
            round(score, 3),
            self.weight_citation,
            f"{len(cited)} verified citation(s), {precision:.0%} precision",
        )

    def _currency_factor(
        self, chunks: Sequence[RetrievedChunk], warnings: list[str]
    ) -> ConfidenceFactor:
        in_force = sum(1 for chunk in chunks if chunk.is_current)
        unknown = sum(1 for chunk in chunks if chunk.document_status is DocumentStatus.UNKNOWN)
        stale = len(chunks) - in_force - unknown

        if stale:
            warnings.append(f"{stale} cited excerpt(s) come from superseded or repealed bylaws")
        if unknown:
            warnings.append(f"{unknown} cited excerpt(s) have unconfirmed in-force status")

        score = in_force / len(chunks) if chunks else 0.0
        # Unknown is not neutral: it means currency could not be established.
        score += 0.4 * (unknown / len(chunks)) if chunks else 0.0

        return ConfidenceFactor(
            "currency",
            round(min(1.0, score), 3),
            self.weight_currency,
            f"{in_force}/{len(chunks)} excerpts confirmed in force",
        )

    def _corroboration_factor(
        self,
        cited: Sequence[RetrievedChunk],
        has_conflicts: bool,
        warnings: list[str],
    ) -> ConfidenceFactor:
        if has_conflicts:
            warnings.append("the cited excerpts disagree with one another")
            return ConfidenceFactor(
                "corroboration", 0.25, self.weight_corroboration, "conflicting excerpts"
            )

        distinct_sections = {
            (chunk.document_id, chunk.section_number) for chunk in cited if chunk.section_number
        }
        count = len(distinct_sections)

        if count == 0:
            return ConfidenceFactor(
                "corroboration", 0.2, self.weight_corroboration, "no identified section"
            )
        if count == 1:
            return ConfidenceFactor(
                "corroboration", 0.6, self.weight_corroboration, "a single section"
            )
        return ConfidenceFactor(
            "corroboration",
            1.0 if count >= 3 else 0.85,
            self.weight_corroboration,
            f"{count} sections agree",
        )

    def _margin_factor(
        self, chunks: Sequence[RetrievedChunk], warnings: list[str]
    ) -> ConfidenceFactor:
        """How decisively the top result beat the rest.

        A flat score distribution means retrieval found nothing that clearly
        matched, even if it returned the requested number of results.
        """
        scores = [chunk.final_score for chunk in chunks]
        if len(scores) < 2:
            return ConfidenceFactor(
                "margin", 0.5, self.weight_margin, "too few candidates to compare"
            )

        ordered = sorted(scores, reverse=True)
        best = ordered[0]
        rest = sum(ordered[1:]) / len(ordered[1:])

        if best <= 0:
            return ConfidenceFactor("margin", 0.2, self.weight_margin, "no positive scores")

        margin = max(0.0, (best - rest) / abs(best))
        if margin < 0.05:
            warnings.append("retrieval scores were flat — no excerpt clearly matched")

        return ConfidenceFactor(
            "margin",
            round(min(1.0, margin * 3), 3),
            self.weight_margin,
            f"top result {margin:.0%} above the mean",
        )

    def _source_quality_factor(
        self, chunks: Sequence[RetrievedChunk], warnings: list[str]
    ) -> ConfidenceFactor:
        if not chunks:
            return ConfidenceFactor("source_quality", 0.0, self.weight_source_quality, "none")

        ocr_count = sum(1 for chunk in chunks if chunk.from_ocr)
        if ocr_count:
            warnings.append(f"{ocr_count} cited excerpt(s) come from OCR and may contain errors")

        mean_extraction = sum(chunk.extraction_confidence for chunk in chunks) / len(chunks)
        ocr_penalty = 0.35 * (ocr_count / len(chunks))

        return ConfidenceFactor(
            "source_quality",
            round(max(0.0, mean_extraction - ocr_penalty), 3),
            self.weight_source_quality,
            f"mean extraction quality {mean_extraction:.0%}, {ocr_count} OCR source(s)",
        )

    def _specificity_factor(
        self,
        municipality_resolved: bool,
        cited: Sequence[RetrievedChunk],
        warnings: list[str],
    ) -> ConfidenceFactor:
        score = 0.0
        details: list[str] = []

        if municipality_resolved:
            score += 0.5
            details.append("municipality identified")
        else:
            warnings.append(
                "no municipality was identified — the answer may not apply to your city"
            )

        if any(chunk.section_number for chunk in cited):
            score += 0.5
            details.append("exact section found")
        else:
            warnings.append("no specific section could be identified")

        return ConfidenceFactor(
            "specificity",
            score,
            self.weight_specificity,
            ", ".join(details) or "neither municipality nor section identified",
        )

    # -- presentation --------------------------------------------------------

    def _band_for(self, score: float) -> ConfidenceBand:
        if score >= self.high_threshold:
            return ConfidenceBand.HIGH
        if score >= self.medium_threshold:
            return ConfidenceBand.MEDIUM
        if score >= self.insufficient_threshold:
            return ConfidenceBand.LOW
        return ConfidenceBand.INSUFFICIENT

    @staticmethod
    def _explain(band: ConfidenceBand, factors: Sequence[ConfidenceFactor]) -> str:
        """One sentence a non-expert can act on."""
        weakest = min(factors, key=lambda factor: factor.score) if factors else None

        if band is ConfidenceBand.HIGH:
            return (
                "High confidence: the answer is drawn from current bylaw text with "
                "verified citations to specific sections."
            )
        if band is ConfidenceBand.MEDIUM:
            detail = f" Weakest signal: {weakest.detail}." if weakest else ""
            return (
                "Medium confidence: the answer is supported, but verify against the "
                f"municipality before acting on it.{detail}"
            )
        if band is ConfidenceBand.LOW:
            detail = f" Weakest signal: {weakest.detail}." if weakest else ""
            return (
                "Low confidence: treat this as a starting point only and confirm "
                f"with the municipality.{detail}"
            )
        return (
            "Insufficient evidence: the indexed bylaws do not clearly answer this. "
            "Contact the municipality directly."
        )


_BAND_ORDER = {
    ConfidenceBand.INSUFFICIENT: 0,
    ConfidenceBand.LOW: 1,
    ConfidenceBand.MEDIUM: 2,
    ConfidenceBand.HIGH: 3,
}


def _band_rank(band: ConfidenceBand) -> int:
    return _BAND_ORDER[band]
