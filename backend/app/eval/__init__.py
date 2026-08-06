"""Evaluation harness.

The regression suite for answer quality. Retrieval, citation and currency are
measured separately so a drop tells you which part of the pipeline moved.

Run against a built index::

    pytest tests/eval -m eval

Cases live in :mod:`app.eval.dataset`. A case without ``verified_by`` exercises
the pipeline but is excluded from accuracy figures — a golden set full of
guesses measures nothing.
"""

from app.eval.dataset import EvalCase, EvalKind, EvalSuite, seed_suite
from app.eval.metrics import CaseResult, SuiteReport, evaluate_case

__all__ = [
    "CaseResult",
    "EvalCase",
    "EvalKind",
    "EvalSuite",
    "SuiteReport",
    "evaluate_case",
    "seed_suite",
]
