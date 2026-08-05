"""Milestone 1 production validation.

Measures the system against a real corpus. Distinct from ``app.eval``, which
asserts known-correct answers and needs human-verified ground truth; this
measures behaviour that can be checked without knowing the right answer, and
produces a worksheet for the part that cannot.
"""

from app.validation.harness import (
    IngestionMetrics,
    QuestionRun,
    ValidationHarness,
    ValidationReport,
    render_report,
)
from app.validation.questions import VALIDATION_QUESTIONS, ValidationQuestion
from app.validation.thresholds import evaluate_thresholds, milestone_passed

__all__ = [
    "VALIDATION_QUESTIONS",
    "IngestionMetrics",
    "QuestionRun",
    "ValidationHarness",
    "ValidationQuestion",
    "ValidationReport",
    "evaluate_thresholds",
    "milestone_passed",
    "render_report",
]
