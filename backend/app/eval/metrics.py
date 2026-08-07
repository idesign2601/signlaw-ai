"""Evaluation metrics.

Three things are measured separately, because they fail for different reasons
and conflating them hides which part of the pipeline broke.

**Retrieval accuracy** — did the correct section reach the model? Everything
downstream is bounded by this: an answer cannot cite a clause retrieval never
surfaced. Measured as recall of the acceptable sections within the top-k.

**Citation accuracy** — of the citations the answer made, how many point at an
acceptable section, and does each quote actually appear in the cited text?
Precision matters more than recall here: a confident citation to the wrong
clause is worse than a missing one.

**Staleness rate** — the fraction of runs citing superseded or repealed text.
Scored separately and reported as a hard count rather than folded into an
average, because one repealed citation is a defect, not a rounding error.

Behavioural cases (ambiguity, out-of-scope, abstention) are pass/fail: the
question is whether the system declined when it should have.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.eval.dataset import EvalCase, EvalKind

__all__ = ["CaseResult", "SuiteReport", "evaluate_case"]


def _normalise_section(section: str) -> str:
    """Compare section numbers ignoring punctuation and case.

    ``5.3(b)``, ``5.3 (b)`` and ``s. 5.3(b)`` are the same clause written three
    ways across municipal citation styles.
    """
    return section.lower().replace("s.", "").replace("section", "").replace(" ", "").strip()


@dataclass(frozen=True, slots=True)
class CaseResult:
    """The outcome of one evaluation case."""

    case_id: str
    kind: EvalKind

    # Retrieval
    retrieved_expected_section: bool = False
    retrieved_expected_municipality: bool = False
    retrieval_rank: int | None = None

    # Citation
    citations_made: int = 0
    citations_correct: int = 0
    quotes_verified: int = 0

    # Currency — a hard failure, not a deduction.
    cited_stale_document: bool = False
    stale_citations: tuple[str, ...] = ()

    # Behaviour
    abstained: bool = False
    asked_clarification: bool = False
    behaviour_correct: bool = True

    # Content
    contains_expected: bool = True
    contains_forbidden: bool = False

    confidence: float = 0.0
    confidence_band: str = ""
    latency_ms: int = 0
    error: str | None = None

    @property
    def citation_precision(self) -> float:
        return self.citations_correct / self.citations_made if self.citations_made else 0.0

    @property
    def passed(self) -> bool:
        """Whether this case is a pass overall.

        Citing stale text fails a case outright regardless of how good the
        answer otherwise was.
        """
        if self.error is not None:
            return False
        if self.cited_stale_document:
            return False
        if not self.behaviour_correct:
            return False
        if self.contains_forbidden:
            return False
        if self.kind in _BEHAVIOURAL_KINDS:
            return True
        return (
            self.retrieved_expected_section
            and self.citation_precision >= 0.5
            and self.contains_expected
        )


_BEHAVIOURAL_KINDS = frozenset({EvalKind.AMBIGUITY, EvalKind.OUT_OF_SCOPE, EvalKind.ABSTENTION})


def evaluate_case(
    case: EvalCase,
    *,
    retrieved_sections: Sequence[str],
    retrieved_municipalities: Sequence[str],
    cited_sections: Sequence[str],
    cited_statuses: Sequence[str],
    quotes_verified: int,
    answer_text: str,
    abstained: bool,
    asked_clarification: bool,
    confidence: float,
    confidence_band: str,
    latency_ms: int = 0,
    error: str | None = None,
) -> CaseResult:
    """Score one case against what the system actually did."""
    if error is not None:
        return CaseResult(case_id=case.id, kind=case.kind, error=error)

    # --- behaviour ---------------------------------------------------------
    if case.should_ask_clarification:
        behaviour_correct = asked_clarification
    elif case.should_abstain:
        behaviour_correct = abstained
    else:
        # A case expecting a real answer fails if the system declined.
        behaviour_correct = not abstained and not asked_clarification

    # --- retrieval ---------------------------------------------------------
    acceptable = {_normalise_section(section) for section in case.acceptable_sections}
    normalised_retrieved = [_normalise_section(section) for section in retrieved_sections]

    retrieved_section = bool(acceptable & set(normalised_retrieved)) if acceptable else True
    rank = next(
        (
            index
            for index, section in enumerate(normalised_retrieved, start=1)
            if section in acceptable
        ),
        None,
    )

    expected_cities = set(case.expected_municipalities)
    retrieved_city = (
        bool(expected_cities & set(retrieved_municipalities)) if expected_cities else True
    )

    # --- citations ---------------------------------------------------------
    normalised_cited = [_normalise_section(section) for section in cited_sections]
    correct = (
        sum(1 for section in normalised_cited if section in acceptable)
        if acceptable
        else len(normalised_cited)
    )

    stale = tuple(
        section
        for section, status in zip(cited_sections, cited_statuses, strict=False)
        if status in {"superseded", "repealed"}
    )

    # --- content -----------------------------------------------------------
    lowered = answer_text.lower()
    contains_expected = all(
        fragment.lower() in lowered for fragment in case.expected_answer_contains
    )
    contains_forbidden = any(fragment.lower() in lowered for fragment in case.must_not_contain)

    return CaseResult(
        case_id=case.id,
        kind=case.kind,
        retrieved_expected_section=retrieved_section,
        retrieved_expected_municipality=retrieved_city,
        retrieval_rank=rank,
        citations_made=len(cited_sections),
        citations_correct=correct,
        quotes_verified=quotes_verified,
        cited_stale_document=bool(stale),
        stale_citations=stale,
        abstained=abstained,
        asked_clarification=asked_clarification,
        behaviour_correct=behaviour_correct,
        contains_expected=contains_expected,
        contains_forbidden=contains_forbidden,
        confidence=confidence,
        confidence_band=confidence_band,
        latency_ms=latency_ms,
    )


@dataclass
class SuiteReport:
    """Aggregate results across a run."""

    results: list[CaseResult] = field(default_factory=list)

    # -- headline numbers ----------------------------------------------------

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def retrieval_accuracy(self) -> float:
        """Fraction of content cases where an acceptable section was retrieved."""
        content = [r for r in self.results if r.kind not in _BEHAVIOURAL_KINDS]
        if not content:
            return 0.0
        return sum(1 for r in content if r.retrieved_expected_section) / len(content)

    @property
    def citation_accuracy(self) -> float:
        """Mean citation precision across cases that made citations."""
        cited = [r for r in self.results if r.citations_made]
        if not cited:
            return 0.0
        return sum(r.citation_precision for r in cited) / len(cited)

    @property
    def staleness_failures(self) -> int:
        """Runs citing superseded or repealed text. Target: zero."""
        return sum(1 for result in self.results if result.cited_stale_document)

    @property
    def behaviour_accuracy(self) -> float:
        """Fraction of cases where the system declined or answered as it should."""
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.behaviour_correct) / len(self.results)

    @property
    def mean_confidence(self) -> float:
        if not self.results:
            return 0.0
        return sum(result.confidence for result in self.results) / len(self.results)

    @property
    def p95_latency_ms(self) -> int:
        if not self.results:
            return 0
        ordered = sorted(result.latency_ms for result in self.results)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return ordered[index]

    @property
    def errors(self) -> int:
        return sum(1 for result in self.results if result.error is not None)

    # -- calibration ---------------------------------------------------------

    def confidence_calibration(self) -> dict[str, dict[str, float | int]]:
        """Pass rate within each confidence band.

        The check that makes confidence meaningful: high-confidence answers
        should pass far more often than low-confidence ones. If they do not,
        the score is decorative and users are being misled by it.
        """
        buckets: dict[str, list[CaseResult]] = {}
        for result in self.results:
            buckets.setdefault(result.confidence_band or "unknown", []).append(result)

        return {
            band: {
                "count": len(items),
                "pass_rate": round(sum(1 for item in items if item.passed) / len(items), 3),
            }
            for band, items in sorted(buckets.items())
        }

    def failures(self) -> list[CaseResult]:
        return [result for result in self.results if not result.passed]

    def as_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 3),
            "retrieval_accuracy": round(self.retrieval_accuracy, 3),
            "citation_accuracy": round(self.citation_accuracy, 3),
            "behaviour_accuracy": round(self.behaviour_accuracy, 3),
            "staleness_failures": self.staleness_failures,
            "errors": self.errors,
            "mean_confidence": round(self.mean_confidence, 3),
            "p95_latency_ms": self.p95_latency_ms,
            "calibration": self.confidence_calibration(),
            "failed_cases": [result.case_id for result in self.failures()],
        }

    def render(self) -> str:
        """Console summary."""
        lines = [
            "SignLaw AI — evaluation",
            "=" * 56,
            f"  cases                 {self.total}",
            f"  passed                {self.passed} ({self.pass_rate:.0%})",
            f"  retrieval accuracy    {self.retrieval_accuracy:.0%}",
            f"  citation accuracy     {self.citation_accuracy:.0%}",
            f"  behaviour accuracy    {self.behaviour_accuracy:.0%}",
            f"  stale citations       {self.staleness_failures}"
            + ("  <-- MUST BE ZERO" if self.staleness_failures else ""),
            f"  errors                {self.errors}",
            f"  p95 latency           {self.p95_latency_ms} ms",
            "",
            "  confidence calibration",
        ]
        for band, stats in self.confidence_calibration().items():
            lines.append(f"    {band:<14} {stats['count']:>3} cases, {stats['pass_rate']:.0%} pass")

        failures = self.failures()
        if failures:
            lines.extend(["", "  failures"])
            lines.extend(f"    {result.case_id}: {_failure_reason(result)}" for result in failures)

        return "\n".join(lines)


def _failure_reason(result: CaseResult) -> str:
    if result.error:
        return f"error — {result.error}"
    if result.cited_stale_document:
        return f"cited superseded/repealed text ({', '.join(result.stale_citations)})"
    if not result.behaviour_correct:
        if result.abstained:
            return "abstained when an answer was expected"
        return "answered when it should have declined or asked"
    if result.contains_forbidden:
        return "answer contained forbidden content"
    if not result.retrieved_expected_section:
        return "expected section was never retrieved"
    if result.citation_precision < 0.5:
        return f"citation precision {result.citation_precision:.0%}"
    if not result.contains_expected:
        return "answer omitted expected content"
    return "unknown"
