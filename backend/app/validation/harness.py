"""Milestone 1 validation harness.

Measures the six metric groups against a real corpus and writes a report.

**What it measures automatically** — everything that does not require knowing
the correct answer:

* ingestion success, per-document timing, pages, chunks, OCR rate
* embedding throughput
* retrieval and answer latency percentiles
* *citation verifiability*: whether each quote actually appears in the chunk it
  was drawn from
* *retrieval precision proxy*: whether retrieval stayed inside the municipality
  that was asked about
* abstention rate, split by whether abstaining was correct
* confidence distribution

**What it cannot measure alone** — anything needing ground truth:

* whether an answer is factually right
* whether the cited section is the *correct* section
* whether the confidence score is calibrated

Confidence calibration in particular needs correctness labels, and correctness
labels need a person with the bylaw open. So the harness emits a spot-check
worksheet with the answers, citations and confidence bands laid out for review,
rather than producing a calibration number it cannot honestly compute.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.services.rag_service import AnswerOutcome, AnswerResult, RagService
from app.validation.questions import VALIDATION_QUESTIONS, ValidationQuestion

__all__ = ["IngestionMetrics", "ValidationHarness", "ValidationReport", "QuestionRun"]

logger = get_logger(__name__)


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


@dataclass
class IngestionMetrics:
    """What ingestion produced, per document."""

    filename: str
    succeeded: bool
    duration_s: float = 0.0
    pages: int = 0
    chunks: int = 0
    sections: int = 0
    tables: int = 0
    ocr_pages: int = 0
    municipality: str | None = None
    bylaw_number: str | None = None
    metadata_confidence: float = 0.0
    error: str | None = None

    @property
    def chunks_per_second(self) -> float:
        return self.chunks / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def sections_detected(self) -> bool:
        """Whether the section parser found any hierarchy.

        Zero sections means every chunk from this document is uncitable at
        clause level — the single most consequential ingestion failure, and one
        that does not raise.
        """
        return self.sections > 0


@dataclass
class QuestionRun:
    """One question's outcome."""

    question: ValidationQuestion
    outcome: AnswerOutcome
    answer: str = ""
    total_ms: int = 0
    retrieval_ms: int = 0
    generation_ms: int = 0

    citations: int = 0
    # Citations whose quote was found verbatim in the chunk it came from.
    # Measurable without ground truth; the strongest automatic signal available.
    verified_citations: int = 0
    cited_sections: tuple[str, ...] = ()
    cited_municipalities: tuple[str, ...] = ()
    cited_statuses: tuple[str, ...] = ()

    retrieved_chunks: int = 0
    retrieved_municipalities: tuple[str, ...] = ()

    confidence: float = 0.0
    confidence_band: str = ""
    error: str | None = None

    @property
    def abstained(self) -> bool:
        return self.outcome not in {
            AnswerOutcome.ANSWERED,
            AnswerOutcome.NEEDS_CLARIFICATION,
        }

    @property
    def behaved_correctly(self) -> bool:
        """Whether the system answered or declined as the question intended."""
        if self.question.expect_clarification:
            return self.outcome is AnswerOutcome.NEEDS_CLARIFICATION
        if self.question.expect_abstention:
            return self.abstained
        return self.outcome is AnswerOutcome.ANSWERED

    @property
    def retrieval_precision(self) -> float | None:
        """Fraction of retrieved chunks from the expected municipality.

        A proxy, not true precision: it measures scoping, not relevance. A low
        value means the filter leaked, which is worth catching on its own.
        """
        expected = set(self.question.expected_municipalities)
        if not expected or not self.retrieved_municipalities:
            return None
        hits = sum(1 for slug in self.retrieved_municipalities if slug in expected)
        return hits / len(self.retrieved_municipalities)

    @property
    def cited_stale(self) -> bool:
        return any(
            status in {"superseded", "repealed"} for status in self.cited_statuses
        )

    @property
    def citation_verification_rate(self) -> float | None:
        if not self.citations:
            return None
        return self.verified_citations / self.citations


@dataclass
class ValidationReport:
    """Everything Milestone 1 measured."""

    started_at: datetime
    finished_at: datetime | None = None
    corpus: list[IngestionMetrics] = field(default_factory=list)
    runs: list[QuestionRun] = field(default_factory=list)
    embedding_model: str = ""
    llm_model: str = ""
    collection: str = ""

    # -- ingestion -----------------------------------------------------------

    @property
    def documents_attempted(self) -> int:
        return len(self.corpus)

    @property
    def documents_ingested(self) -> int:
        return sum(1 for doc in self.corpus if doc.succeeded)

    @property
    def ingestion_success_rate(self) -> float:
        return (
            self.documents_ingested / self.documents_attempted
            if self.documents_attempted
            else 0.0
        )

    @property
    def documents_without_sections(self) -> list[str]:
        """Documents whose chunks cannot carry a clause-level citation."""
        return [
            doc.filename
            for doc in self.corpus
            if doc.succeeded and not doc.sections_detected
        ]

    @property
    def total_chunks(self) -> int:
        return sum(doc.chunks for doc in self.corpus)

    @property
    def total_pages(self) -> int:
        return sum(doc.pages for doc in self.corpus)

    @property
    def ocr_page_rate(self) -> float:
        total = self.total_pages
        return sum(doc.ocr_pages for doc in self.corpus) / total if total else 0.0

    @property
    def embedding_throughput(self) -> float:
        """Chunks embedded per second across the whole corpus."""
        elapsed = sum(doc.duration_s for doc in self.corpus if doc.succeeded)
        return self.total_chunks / elapsed if elapsed > 0 else 0.0

    # -- latency -------------------------------------------------------------

    def _latencies(self, attribute: str) -> list[float]:
        return [
            float(getattr(run, attribute))
            for run in self.runs
            if run.error is None and getattr(run, attribute)
        ]

    @property
    def retrieval_p50_ms(self) -> float:
        return _percentile(self._latencies("retrieval_ms"), 0.50)

    @property
    def retrieval_p95_ms(self) -> float:
        return _percentile(self._latencies("retrieval_ms"), 0.95)

    @property
    def answer_p50_ms(self) -> float:
        return _percentile(self._latencies("total_ms"), 0.50)

    @property
    def answer_p95_ms(self) -> float:
        return _percentile(self._latencies("total_ms"), 0.95)

    @property
    def generation_p50_ms(self) -> float:
        return _percentile(self._latencies("generation_ms"), 0.50)

    # -- citations -----------------------------------------------------------

    @property
    def citation_verification_rate(self) -> float:
        """Fraction of emitted citations whose quote was found in its source.

        Measurable without ground truth. Anything below 1.0 means the verifier
        caught fabrication — which is the system working, but the rate is worth
        watching because it tracks model quality.
        """
        total = sum(run.citations for run in self.runs)
        verified = sum(run.verified_citations for run in self.runs)
        return verified / total if total else 0.0

    @property
    def answers_with_citations(self) -> float:
        answered = [r for r in self.runs if r.outcome is AnswerOutcome.ANSWERED]
        if not answered:
            return 0.0
        return sum(1 for r in answered if r.citations) / len(answered)

    @property
    def stale_citation_count(self) -> int:
        """Answers resting on superseded or repealed text. Must be zero."""
        return sum(1 for run in self.runs if run.cited_stale)

    # -- behaviour -----------------------------------------------------------

    @property
    def abstention_rate(self) -> float:
        return (
            sum(1 for run in self.runs if run.abstained) / len(self.runs)
            if self.runs
            else 0.0
        )

    @property
    def correct_abstention_rate(self) -> float:
        """Of the questions that *should* be declined, how many were."""
        should = [r for r in self.runs if r.question.expect_abstention]
        if not should:
            return 0.0
        return sum(1 for r in should if r.abstained) / len(should)

    @property
    def false_abstention_rate(self) -> float:
        """Of the answerable questions, how many were wrongly declined.

        The metric that catches over-caution. A system that abstains on
        everything scores perfectly on safety and is useless.
        """
        answerable = [r for r in self.runs if r.question.expects_answer]
        if not answerable:
            return 0.0
        return sum(1 for r in answerable if r.abstained) / len(answerable)

    @property
    def behaviour_accuracy(self) -> float:
        return (
            sum(1 for run in self.runs if run.behaved_correctly) / len(self.runs)
            if self.runs
            else 0.0
        )

    # -- retrieval -----------------------------------------------------------

    @property
    def retrieval_precision(self) -> float:
        """Mean municipality-scoping precision across scoped questions."""
        values = [
            run.retrieval_precision
            for run in self.runs
            if run.retrieval_precision is not None
        ]
        return statistics.mean(values) if values else 0.0

    @property
    def scope_leaks(self) -> list[str]:
        """Questions where retrieval returned another municipality's text."""
        return [
            run.question.id
            for run in self.runs
            if run.retrieval_precision is not None and run.retrieval_precision < 1.0
        ]

    # -- confidence ----------------------------------------------------------

    def confidence_distribution(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self.runs:
            band = run.confidence_band or "none"
            counts[band] = counts.get(band, 0) + 1
        return dict(sorted(counts.items()))

    # -- output --------------------------------------------------------------

    @property
    def errors(self) -> int:
        return sum(1 for run in self.runs if run.error is not None)

    def as_dict(self) -> dict[str, object]:
        return {
            "run": {
                "started_at": self.started_at.isoformat(),
                "finished_at": (
                    self.finished_at.isoformat() if self.finished_at else None
                ),
                "embedding_model": self.embedding_model,
                "llm_model": self.llm_model,
                "collection": self.collection,
            },
            "ingestion": {
                "attempted": self.documents_attempted,
                "ingested": self.documents_ingested,
                "success_rate": round(self.ingestion_success_rate, 3),
                "pages": self.total_pages,
                "chunks": self.total_chunks,
                "ocr_page_rate": round(self.ocr_page_rate, 3),
                "embedding_chunks_per_second": round(self.embedding_throughput, 2),
                "documents_without_sections": self.documents_without_sections,
                "per_document": [
                    {
                        "filename": doc.filename,
                        "succeeded": doc.succeeded,
                        "municipality": doc.municipality,
                        "bylaw_number": doc.bylaw_number,
                        "metadata_confidence": doc.metadata_confidence,
                        "pages": doc.pages,
                        "sections": doc.sections,
                        "chunks": doc.chunks,
                        "tables": doc.tables,
                        "ocr_pages": doc.ocr_pages,
                        "duration_s": round(doc.duration_s, 2),
                        "error": doc.error,
                    }
                    for doc in self.corpus
                ],
            },
            "latency_ms": {
                "retrieval_p50": round(self.retrieval_p50_ms),
                "retrieval_p95": round(self.retrieval_p95_ms),
                "generation_p50": round(self.generation_p50_ms),
                "answer_p50": round(self.answer_p50_ms),
                "answer_p95": round(self.answer_p95_ms),
            },
            "citations": {
                "verification_rate": round(self.citation_verification_rate, 3),
                "answers_with_citations": round(self.answers_with_citations, 3),
                "stale_citations": self.stale_citation_count,
            },
            "behaviour": {
                "abstention_rate": round(self.abstention_rate, 3),
                "correct_abstention_rate": round(self.correct_abstention_rate, 3),
                "false_abstention_rate": round(self.false_abstention_rate, 3),
                "behaviour_accuracy": round(self.behaviour_accuracy, 3),
            },
            "retrieval": {
                "municipality_precision": round(self.retrieval_precision, 3),
                "scope_leaks": self.scope_leaks,
            },
            "confidence": {
                "distribution": self.confidence_distribution(),
                "calibration": (
                    "NOT MEASURED — requires human correctness labels. "
                    "See the spot-check worksheet."
                ),
            },
            "errors": self.errors,
        }

    def spot_check_worksheet(self) -> str:
        """Markdown worksheet for the human review the harness cannot do.

        Factual correctness and confidence calibration both need someone with
        the bylaw open. This lays the answers out so that review is quick and
        the verdicts feed straight back into the golden set.
        """
        lines = [
            "# Spot-check worksheet",
            "",
            "The harness cannot judge whether an answer is factually correct, "
            "so it cannot compute confidence calibration. Open each bylaw, mark "
            "each answer, and record the result.",
            "",
            "Mark `correct` / `wrong` / `unsupported`. Anything marked `wrong` "
            "at HIGH confidence is a calibration defect and blocks the "
            "milestone.",
            "",
            "| # | Question | Confidence | Cited section(s) | Page(s) | Verdict |",
            "|---|---|---|---|---|---|",
        ]

        for index, run in enumerate(
            [r for r in self.runs if r.outcome is AnswerOutcome.ANSWERED], start=1
        ):
            sections = ", ".join(run.cited_sections) or "—"
            lines.append(
                f"| {index} | {run.question.question} | "
                f"{run.confidence_band.upper()} ({run.confidence:.2f}) | "
                f"{sections} | — | |"
            )

        lines.extend(
            [
                "",
                "## Answers in full",
                "",
            ]
        )
        for index, run in enumerate(
            [r for r in self.runs if r.outcome is AnswerOutcome.ANSWERED], start=1
        ):
            lines.extend(
                [
                    f"### {index}. {run.question.question}",
                    "",
                    f"**Confidence:** {run.confidence_band.upper()} "
                    f"({run.confidence:.2f})",
                    "",
                    run.answer,
                    "",
                    "**Citations:**",
                    "",
                ]
            )
            if run.cited_sections:
                lines.extend(
                    f"- s. {section}" for section in run.cited_sections
                )
            else:
                lines.append("- none")
            lines.append("")

        return "\n".join(lines)


@dataclass
class ValidationHarness:
    """Runs the validation questions and collects metrics."""

    service: RagService

    async def run(
        self,
        questions: Sequence[ValidationQuestion] = VALIDATION_QUESTIONS,
        *,
        corpus: Sequence[IngestionMetrics] = (),
        embedding_model: str = "",
        llm_model: str = "",
        collection: str = "",
    ) -> ValidationReport:
        """Execute every question sequentially and return the report.

        Sequential on purpose: local generation is the bottleneck, and running
        several at once against one Ollama measures queueing rather than the
        pipeline.
        """
        report = ValidationReport(
            started_at=datetime.now(UTC),
            corpus=list(corpus),
            embedding_model=embedding_model,
            llm_model=llm_model,
            collection=collection,
        )

        for index, question in enumerate(questions, start=1):
            logger.info(
                "validation_question",
                id=question.id,
                index=index,
                total=len(questions),
            )
            report.runs.append(await self._run_one(question))

        report.finished_at = datetime.now(UTC)
        logger.info(
            "validation_completed",
            questions=len(report.runs),
            behaviour_accuracy=round(report.behaviour_accuracy, 3),
            stale_citations=report.stale_citation_count,
        )
        return report

    async def _run_one(self, question: ValidationQuestion) -> QuestionRun:
        started = time.perf_counter()
        try:
            result = await self.service.answer(question.question)
        except Exception as exc:  # one bad question must not stop the run
            logger.exception("validation_question_errored", id=question.id)
            return QuestionRun(
                question=question,
                outcome=AnswerOutcome.NO_RELEVANT_BYLAW,
                total_ms=int((time.perf_counter() - started) * 1000),
                error=str(exc),
            )

        return self._collect(question, result)

    @staticmethod
    def _collect(question: ValidationQuestion, result: AnswerResult) -> QuestionRun:
        trace = result.trace

        retrieved_municipalities = (
            tuple(
                str(score.get("municipality") or "")
                for score in trace.retrieval_scores
                if score.get("municipality")
            )
            if trace
            else ()
        )

        verification = trace.verification if trace else {}
        raw_verified = verification.get("valid_citations", len(result.citations))
        verified = raw_verified if isinstance(raw_verified, int) else len(result.citations)

        return QuestionRun(
            question=question,
            outcome=result.outcome,
            answer=result.answer,
            total_ms=trace.total_ms if trace else 0,
            retrieval_ms=trace.retrieval_ms if trace else 0,
            generation_ms=trace.generation_ms if trace else 0,
            citations=len(result.citations),
            verified_citations=verified,
            cited_sections=tuple(
                citation.section or "" for citation in result.citations
            ),
            cited_municipalities=tuple(
                citation.municipality or "" for citation in result.citations
            ),
            cited_statuses=tuple(
                citation.amendment_status for citation in result.citations
            ),
            retrieved_chunks=len(trace.retrieved_chunk_ids) if trace else 0,
            retrieved_municipalities=retrieved_municipalities,
            confidence=result.confidence_score,
            confidence_band=result.band.value,
        )


def render_report(report: ValidationReport) -> str:
    """Console summary against the Milestone 1 thresholds."""
    from app.validation.thresholds import evaluate_thresholds

    checks = evaluate_thresholds(report)
    lines = [
        "",
        "SignLaw AI — Milestone 1 validation",
        "=" * 68,
        f"  embedding model     {report.embedding_model}",
        f"  generation model    {report.llm_model}",
        f"  collection          {report.collection}",
        "",
        "  INGESTION",
        f"    documents         {report.documents_ingested}/"
        f"{report.documents_attempted} "
        f"({report.ingestion_success_rate:.0%})",
        f"    pages             {report.total_pages}",
        f"    chunks            {report.total_chunks}",
        f"    OCR page rate     {report.ocr_page_rate:.1%}",
        f"    embedding rate    {report.embedding_throughput:.1f} chunks/s",
        "",
        "  LATENCY",
        f"    retrieval p50/p95 {report.retrieval_p50_ms:.0f} / "
        f"{report.retrieval_p95_ms:.0f} ms",
        f"    generation p50    {report.generation_p50_ms:.0f} ms",
        f"    answer p50/p95    {report.answer_p50_ms:.0f} / "
        f"{report.answer_p95_ms:.0f} ms",
        "",
        "  CITATIONS",
        f"    verification rate {report.citation_verification_rate:.1%}",
        f"    answers cited     {report.answers_with_citations:.1%}",
        f"    stale citations   {report.stale_citation_count}",
        "",
        "  BEHAVIOUR",
        f"    abstention rate   {report.abstention_rate:.1%}",
        f"    correct abstain   {report.correct_abstention_rate:.1%}",
        f"    false abstain     {report.false_abstention_rate:.1%}",
        f"    behaviour acc.    {report.behaviour_accuracy:.1%}",
        "",
        "  RETRIEVAL",
        f"    municipality prec {report.retrieval_precision:.1%}",
        f"    scope leaks       {len(report.scope_leaks)}",
        "",
        "  CONFIDENCE",
        f"    distribution      {report.confidence_distribution()}",
        "    calibration       NOT MEASURED — needs the spot-check worksheet",
        "",
        "=" * 68,
        "  THRESHOLDS",
    ]

    for check in checks:
        symbol = "pass" if check.passed else "FAIL"
        lines.append(f"    {symbol:<5} {check.name:<28} {check.detail}")

    failed = [check for check in checks if not check.passed]
    lines.extend(
        [
            "=" * 68,
            (
                f"  {len(failed)} threshold(s) failed — milestone NOT passed."
                if failed
                else "  All automatic thresholds passed."
            ),
            "",
            "  Automatic checks do not cover factual correctness or confidence",
            "  calibration. Complete the spot-check worksheet before signing off.",
            "",
        ]
    )
    return "\n".join(lines)
