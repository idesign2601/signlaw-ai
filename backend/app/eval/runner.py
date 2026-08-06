"""Evaluation runner.

Drives the real pipeline over the golden suite and scores what comes back. No
mocks: the point is to measure the system as it actually behaves, including
retrieval, reranking and generation.

Cases run sequentially. Local generation is the bottleneck and running several
at once on one Ollama instance mostly produces contention, so the latency
figures would measure queueing rather than the pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.logging import get_logger
from app.eval.dataset import EvalCase, EvalSuite
from app.eval.metrics import CaseResult, SuiteReport, evaluate_case
from app.services.rag_service import AnswerOutcome, AnswerResult, RagService

__all__ = ["EvalRunner"]

logger = get_logger(__name__)


@dataclass
class EvalRunner:
    """Runs an evaluation suite against a live service."""

    service: RagService

    async def run(self, suite: EvalSuite) -> SuiteReport:
        """Execute every case and return the aggregate report."""
        report = SuiteReport()

        for index, case in enumerate(suite.cases, start=1):
            logger.info(
                "eval_case_started", case_id=case.id, index=index, total=len(suite)
            )
            report.results.append(await self._run_case(case))

        logger.info(
            "eval_completed",
            cases=report.total,
            passed=report.passed,
            retrieval_accuracy=round(report.retrieval_accuracy, 3),
            citation_accuracy=round(report.citation_accuracy, 3),
            staleness_failures=report.staleness_failures,
        )
        return report

    async def _run_case(self, case: EvalCase) -> CaseResult:
        started = time.perf_counter()

        try:
            result = await self.service.answer(case.question)
        except Exception as exc:  # one bad case must not stop the suite
            logger.exception("eval_case_errored", case_id=case.id)
            return evaluate_case(
                case,
                retrieved_sections=(),
                retrieved_municipalities=(),
                cited_sections=(),
                cited_statuses=(),
                quotes_verified=0,
                answer_text="",
                abstained=True,
                asked_clarification=False,
                confidence=0.0,
                confidence_band="",
                error=str(exc),
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        return evaluate_case(
            case,
            retrieved_sections=self._retrieved_sections(result),
            retrieved_municipalities=self._retrieved_municipalities(result),
            cited_sections=[c.section or "" for c in result.citations],
            cited_statuses=[c.amendment_status for c in result.citations],
            quotes_verified=len(result.citations),
            answer_text=result.answer,
            abstained=self._is_abstention(result),
            asked_clarification=(
                result.outcome is AnswerOutcome.NEEDS_CLARIFICATION
            ),
            confidence=result.confidence_score,
            confidence_band=result.band.value,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _is_abstention(result: AnswerResult) -> bool:
        """Whether the system declined to answer.

        A clarification request is not an abstention: it is a different correct
        behaviour, scored separately, because conflating them would let a system
        that always asks for clarification score well on abstention cases.
        """
        return result.outcome not in {
            AnswerOutcome.ANSWERED,
            AnswerOutcome.NEEDS_CLARIFICATION,
        }

    @staticmethod
    def _retrieved_sections(result: AnswerResult) -> list[str]:
        """Sections that reached the model, in rank order.

        Read from the trace rather than the citations, so retrieval accuracy is
        measured independently of whether the model chose to cite them.
        """
        if result.trace is None:
            return []
        return [
            str(score.get("section") or "")
            for score in result.trace.retrieval_scores
            if score.get("section")
        ]

    @staticmethod
    def _retrieved_municipalities(result: AnswerResult) -> list[str]:
        if result.trace is None:
            return []
        return list(
            dict.fromkeys(
                str(score.get("municipality") or "")
                for score in result.trace.retrieval_scores
                if score.get("municipality")
            )
        )
