"""Milestone 1 acceptance thresholds.

Stated as code so "did it pass?" has one answer rather than an argument.

Two are absolute and cannot be traded against anything else:

* **Zero stale citations.** One answer resting on repealed text is a defect.
* **Zero scope leaks on the uncovered-city probe.** Answering a Kelowna
  question from Burnaby's bylaw is the failure this product exists to prevent.

The rest are first-run targets rather than laws of nature. Latency numbers
depend on hardware and model size; if they fail on a laptop with a 14B model,
that is information about the deployment, not necessarily a defect. What must
not be adjusted after seeing the results are the correctness thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.validation.harness import ValidationReport

__all__ = ["ThresholdCheck", "evaluate_thresholds", "THRESHOLDS"]


@dataclass(frozen=True, slots=True)
class ThresholdCheck:
    """One acceptance criterion and its outcome."""

    name: str
    passed: bool
    detail: str
    blocking: bool = True


# Correctness — do not relax these after seeing results.
MIN_INGESTION_SUCCESS = 1.0
MAX_STALE_CITATIONS = 0
MIN_CITATION_VERIFICATION = 0.95
MIN_ANSWERS_WITH_CITATIONS = 0.90
MIN_RETRIEVAL_PRECISION = 0.90
MIN_CORRECT_ABSTENTION = 0.90
MAX_FALSE_ABSTENTION = 0.20
MIN_BEHAVIOUR_ACCURACY = 0.90

# Performance — hardware dependent, adjust with justification.
MAX_RETRIEVAL_P95_MS = 2_000
MAX_ANSWER_P95_MS = 30_000
MIN_EMBEDDING_THROUGHPUT = 5.0

THRESHOLDS = {
    "ingestion_success_rate": MIN_INGESTION_SUCCESS,
    "stale_citations": MAX_STALE_CITATIONS,
    "citation_verification_rate": MIN_CITATION_VERIFICATION,
    "answers_with_citations": MIN_ANSWERS_WITH_CITATIONS,
    "retrieval_precision": MIN_RETRIEVAL_PRECISION,
    "correct_abstention_rate": MIN_CORRECT_ABSTENTION,
    "false_abstention_rate": MAX_FALSE_ABSTENTION,
    "behaviour_accuracy": MIN_BEHAVIOUR_ACCURACY,
    "retrieval_p95_ms": MAX_RETRIEVAL_P95_MS,
    "answer_p95_ms": MAX_ANSWER_P95_MS,
    "embedding_throughput": MIN_EMBEDDING_THROUGHPUT,
}


def evaluate_thresholds(report: ValidationReport) -> list[ThresholdCheck]:
    """Score a report against every acceptance criterion."""
    checks: list[ThresholdCheck] = []

    # --- ingestion ---------------------------------------------------------
    checks.append(
        ThresholdCheck(
            "ingestion success",
            report.ingestion_success_rate >= MIN_INGESTION_SUCCESS,
            f"{report.ingestion_success_rate:.0%} "
            f"(need {MIN_INGESTION_SUCCESS:.0%})",
        )
    )

    # A document with no detected sections yields chunks that cannot carry a
    # clause-level citation. It ingests "successfully" and is useless, so it is
    # checked separately.
    missing = report.documents_without_sections
    checks.append(
        ThresholdCheck(
            "section parsing",
            not missing,
            "all documents produced sections"
            if not missing
            else f"NO sections detected in: {', '.join(missing)}",
        )
    )

    checks.append(
        ThresholdCheck(
            "embedding throughput",
            report.embedding_throughput >= MIN_EMBEDDING_THROUGHPUT,
            f"{report.embedding_throughput:.1f} chunks/s "
            f"(need {MIN_EMBEDDING_THROUGHPUT})",
            blocking=False,
        )
    )

    # --- citations ---------------------------------------------------------
    checks.append(
        ThresholdCheck(
            "no stale citations",
            report.stale_citation_count <= MAX_STALE_CITATIONS,
            f"{report.stale_citation_count} answer(s) cited superseded or "
            f"repealed text (must be 0)",
        )
    )
    checks.append(
        ThresholdCheck(
            "citation verification",
            report.citation_verification_rate >= MIN_CITATION_VERIFICATION,
            f"{report.citation_verification_rate:.1%} of quotes found in source "
            f"(need {MIN_CITATION_VERIFICATION:.0%})",
        )
    )
    checks.append(
        ThresholdCheck(
            "answers carry citations",
            report.answers_with_citations >= MIN_ANSWERS_WITH_CITATIONS,
            f"{report.answers_with_citations:.1%} "
            f"(need {MIN_ANSWERS_WITH_CITATIONS:.0%})",
        )
    )

    # --- retrieval ---------------------------------------------------------
    checks.append(
        ThresholdCheck(
            "municipality precision",
            report.retrieval_precision >= MIN_RETRIEVAL_PRECISION,
            f"{report.retrieval_precision:.1%} "
            f"(need {MIN_RETRIEVAL_PRECISION:.0%})",
        )
    )

    # The single most important probe: a question about a city outside the
    # corpus must not be answered from a city inside it.
    uncovered = next(
        (run for run in report.runs if run.question.id == "unans-uncovered-city"),
        None,
    )
    if uncovered is not None:
        checks.append(
            ThresholdCheck(
                "uncovered city abstains",
                uncovered.abstained,
                "abstained correctly"
                if uncovered.abstained
                else "ANSWERED a Kelowna question from another city's bylaw",
            )
        )

    # --- behaviour ---------------------------------------------------------
    checks.append(
        ThresholdCheck(
            "correct abstention",
            report.correct_abstention_rate >= MIN_CORRECT_ABSTENTION,
            f"{report.correct_abstention_rate:.1%} of should-decline questions "
            f"declined (need {MIN_CORRECT_ABSTENTION:.0%})",
        )
    )
    checks.append(
        ThresholdCheck(
            "not over-cautious",
            report.false_abstention_rate <= MAX_FALSE_ABSTENTION,
            f"{report.false_abstention_rate:.1%} of answerable questions "
            f"declined (max {MAX_FALSE_ABSTENTION:.0%})",
        )
    )
    checks.append(
        ThresholdCheck(
            "behaviour accuracy",
            report.behaviour_accuracy >= MIN_BEHAVIOUR_ACCURACY,
            f"{report.behaviour_accuracy:.1%} "
            f"(need {MIN_BEHAVIOUR_ACCURACY:.0%})",
        )
    )

    # --- performance -------------------------------------------------------
    checks.append(
        ThresholdCheck(
            "retrieval p95",
            report.retrieval_p95_ms <= MAX_RETRIEVAL_P95_MS,
            f"{report.retrieval_p95_ms:.0f} ms (max {MAX_RETRIEVAL_P95_MS})",
            blocking=False,
        )
    )
    checks.append(
        ThresholdCheck(
            "answer p95",
            report.answer_p95_ms <= MAX_ANSWER_P95_MS,
            f"{report.answer_p95_ms:.0f} ms (max {MAX_ANSWER_P95_MS})",
            blocking=False,
        )
    )

    # --- errors ------------------------------------------------------------
    checks.append(
        ThresholdCheck(
            "no errors",
            report.errors == 0,
            f"{report.errors} question(s) raised",
        )
    )

    return checks


def milestone_passed(report: ValidationReport) -> bool:
    """Whether every blocking threshold passed.

    Not sufficient on its own: factual correctness and confidence calibration
    are assessed by human spot check, which this cannot see.
    """
    return all(
        check.passed for check in evaluate_thresholds(report) if check.blocking
    )
