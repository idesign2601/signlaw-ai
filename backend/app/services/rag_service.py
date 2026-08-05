"""End-to-end RAG orchestration.

    question -> route -> retrieve -> rank -> assemble -> generate
             -> verify -> score -> response

No FastAPI import anywhere in this module. It is called from tests and the CLI
today and from HTTP handlers in Phase 5, and keeping the boundary sharp is what
lets the whole pipeline be exercised without a web server.

Every collaborator is injected, so the pipeline can be driven with fakes and no
Postgres, no Ollama and no model weights.

**Failure is a first-class outcome, not an exception.** Five distinct ways this
system can fail to answer, each needing a different response:

* nothing relevant retrieved — say so, do not reason from adjacent bylaws
* municipality unclear — ask, because the wrong Langley is a plausible wrong answer
* only superseded text found — say the text exists but is not current
* in-force documents conflict — surface both rather than silently picking one
* generation unavailable — an infrastructure failure, reported as such

Each returns a structured result the caller can act on, and each is traced.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from app.core.exceptions import (
    EmbeddingError,
    ExternalServiceError,
    IndexNotReadyError,
    LLMError,
    RetrievalError,
)
from app.core.logging import get_logger
from app.db.enums import ConfidenceBand, DocumentStatus, QueryIntent
from app.domain.confidence import ConfidenceReport
from app.domain.query_router import QueryPlan, QueryRouter
from app.rag.prompts import PROMPT_VERSION
from app.rag.results import RetrievalTrace, RetrievedChunk
from app.rag.retriever import RetrievalFilters
from app.rag.synthesizer import AnswerSynthesizer, SynthesizedCitation

__all__ = ["AnswerOutcome", "AnswerResult", "PipelineTrace", "RagService"]

logger = get_logger(__name__)


class AnswerOutcome(StrEnum):
    """How the pipeline resolved. Part of the response contract."""

    ANSWERED = "answered"
    NEEDS_CLARIFICATION = "needs_clarification"
    OUT_OF_SCOPE = "out_of_scope"
    NO_RELEVANT_BYLAW = "no_relevant_bylaw"
    ONLY_OUTDATED = "only_outdated"
    CONFLICTING_AMENDMENTS = "conflicting_amendments"
    UNVERIFIED = "unverified"
    GENERATION_UNAVAILABLE = "generation_unavailable"
    INDEX_NOT_READY = "index_not_ready"

    @property
    def is_answer(self) -> bool:
        return self is AnswerOutcome.ANSWERED

    @property
    def is_infrastructure_failure(self) -> bool:
        """Whether this is our problem rather than a limit of the corpus."""
        return self in {
            AnswerOutcome.GENERATION_UNAVAILABLE,
            AnswerOutcome.INDEX_NOT_READY,
        }


class RetrieverProtocol(Protocol):
    """Hybrid retrieval, as :class:`app.rag.retriever.HybridRetriever` provides."""

    async def retrieve(
        self,
        query: str,
        *,
        filters: RetrievalFilters | None = None,
        top_n: int | None = None,
    ) -> tuple[list[RetrievedChunk], RetrievalTrace]: ...


class TraceSink(Protocol):
    """Persists a completed trace. Optional, so the CLI can run without a DB."""

    async def record(self, trace: PipelineTrace) -> None: ...


@dataclass(frozen=True, slots=True)
class ConflictDetail:
    """Two in-force documents regulating the same section differently."""

    municipality: str | None
    section: str | None
    documents: tuple[str, ...]
    detail: str


@dataclass
class PipelineTrace:
    """Everything one answer did, for audit and debugging.

    Non-negotiable for a legal tool: when an answer is disputed months later,
    this reconstructs which chunks were considered, how they scored, what prompt
    version produced the text and what verification concluded.
    """

    trace_id: str
    question: str
    created_at: datetime

    # Routing
    intent: str = ""
    resolved_municipalities: tuple[str, ...] = ()
    ambiguous_names: tuple[str, ...] = ()

    # Retrieval
    collection: str = ""
    retrieved_chunk_ids: tuple[str, ...] = ()
    retrieval_scores: tuple[dict[str, object], ...] = ()
    dense_candidates: int = 0
    sparse_candidates: int = 0
    fused_candidates: int = 0
    reranked: bool = False
    filters: dict[str, object] = field(default_factory=dict)

    # Generation
    model_used: str = ""
    prompt_version: str = PROMPT_VERSION
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    # Output
    outcome: AnswerOutcome = AnswerOutcome.ANSWERED
    answer: str = ""
    citations: tuple[dict[str, object], ...] = ()
    verification: dict[str, object] = field(default_factory=dict)
    confidence: dict[str, object] = field(default_factory=dict)

    # Timing
    retrieval_ms: int = 0
    generation_ms: int = 0
    total_ms: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "trace_id": self.trace_id,
            "question": self.question,
            "created_at": self.created_at.isoformat(),
            "routing": {
                "intent": self.intent,
                "municipalities": list(self.resolved_municipalities),
                "ambiguous": list(self.ambiguous_names),
            },
            "retrieval": {
                "collection": self.collection,
                "chunk_ids": list(self.retrieved_chunk_ids),
                "scores": list(self.retrieval_scores),
                "dense_candidates": self.dense_candidates,
                "sparse_candidates": self.sparse_candidates,
                "fused_candidates": self.fused_candidates,
                "reranked": self.reranked,
                "filters": self.filters,
                "duration_ms": self.retrieval_ms,
            },
            "generation": {
                "model": self.model_used,
                "prompt_version": self.prompt_version,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "duration_ms": self.generation_ms,
            },
            "outcome": self.outcome.value,
            "answer": self.answer,
            "citations": list(self.citations),
            "verification": self.verification,
            "confidence": self.confidence,
            "total_ms": self.total_ms,
        }


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """What the caller gets back."""

    outcome: AnswerOutcome
    answer: str
    citations: tuple[SynthesizedCitation, ...] = ()
    confidence: ConfidenceReport | None = None
    # Populated when the municipality could not be resolved.
    clarification: str | None = None
    clarification_options: tuple[str, ...] = ()
    conflicts: tuple[ConflictDetail, ...] = ()
    conditions: tuple[str, ...] = ()
    # Documents found but excluded as superseded or repealed.
    outdated_documents: tuple[str, ...] = ()
    trace: PipelineTrace | None = None

    @property
    def answered(self) -> bool:
        return self.outcome.is_answer

    @property
    def band(self) -> ConfidenceBand:
        return self.confidence.band if self.confidence else ConfidenceBand.INSUFFICIENT

    @property
    def confidence_score(self) -> float:
        return self.confidence.score if self.confidence else 0.0


# Messages are written for a sign contractor, not an operator. Each says what
# happened and what to do next.
_MESSAGES = {
    AnswerOutcome.OUT_OF_SCOPE: (
        "That is outside what I can answer. I only answer questions about "
        "municipal sign bylaws in British Columbia, using the indexed bylaw text."
    ),
    AnswerOutcome.NO_RELEVANT_BYLAW: (
        "I could not find anything in the indexed bylaws that addresses this. "
        "That may mean the bylaw is silent on it, or that this municipality's "
        "bylaw has not been indexed. Contact the municipality's planning "
        "department to confirm."
    ),
    AnswerOutcome.INDEX_NOT_READY: (
        "No bylaws have been indexed yet, so there is nothing to search."
    ),
    AnswerOutcome.GENERATION_UNAVAILABLE: (
        "I found relevant bylaw text but could not generate an answer because "
        "the local language model is unavailable. The retrieved sections are "
        "listed below and can be read directly."
    ),
}


@dataclass
class RagService:
    """Runs the full question-to-answer pipeline."""

    retriever: RetrieverProtocol
    synthesizer: AnswerSynthesizer
    router: QueryRouter = field(default_factory=QueryRouter)
    trace_sink: TraceSink | None = None
    # Two in-force documents regulating the same section differently is rare
    # and meaningful. Answering from one of them silently picks a winner.
    abstain_on_conflict: bool = True
    # Whether to re-query without the in-force filter when nothing is found, to
    # distinguish "no such rule" from "the rule exists but is superseded".
    detect_outdated: bool = True

    async def answer(
        self,
        question: str,
        *,
        filters: RetrievalFilters | None = None,
        top_n: int | None = None,
    ) -> AnswerResult:
        """Answer a question. Never raises for an expected failure."""
        started = time.perf_counter()
        trace = PipelineTrace(
            trace_id=uuid.uuid4().hex,
            question=question,
            created_at=datetime.now(UTC),
        )

        plan = self.router.route(question)
        trace.intent = plan.intent.value
        trace.resolved_municipalities = plan.municipality_slugs
        trace.ambiguous_names = plan.ambiguous_names

        # --- routing failures ----------------------------------------------
        if plan.intent is QueryIntent.OUT_OF_SCOPE:
            return await self._finish(
                trace, started, self._simple(AnswerOutcome.OUT_OF_SCOPE, trace)
            )

        if plan.needs_clarification:
            return await self._finish(trace, started, self._clarify(plan, trace))

        # --- retrieval -------------------------------------------------------
        effective_filters = filters or self._filters_for(plan)

        try:
            chunks, retrieval_trace = await self.retriever.retrieve(
                question, filters=effective_filters, top_n=top_n
            )
        except IndexNotReadyError:
            return await self._finish(
                trace, started, self._simple(AnswerOutcome.INDEX_NOT_READY, trace)
            )
        except (RetrievalError, EmbeddingError) as exc:
            logger.error("retrieval_failed", error=exc.message)
            return await self._finish(
                trace, started, self._simple(AnswerOutcome.NO_RELEVANT_BYLAW, trace)
            )

        self._record_retrieval(trace, chunks, retrieval_trace)

        if not chunks:
            return await self._finish(
                trace, started, await self._nothing_found(plan, effective_filters, trace)
            )

        # --- conflicting amendments -----------------------------------------
        conflicts = self._detect_conflicts(chunks)
        if conflicts and self.abstain_on_conflict:
            return await self._finish(trace, started, self._conflicted(conflicts, chunks, trace))

        # --- generation ------------------------------------------------------
        generation_started = time.perf_counter()
        try:
            synthesis = await self.synthesizer.synthesize(
                question,
                chunks,
                is_comparison=plan.is_comparison,
                municipality_resolved=bool(plan.municipalities),
            )
        except (LLMError, ExternalServiceError) as exc:
            logger.error("generation_unavailable", error=str(exc))
            trace.generation_ms = int((time.perf_counter() - generation_started) * 1000)
            return await self._finish(trace, started, self._generation_down(chunks, trace))

        trace.generation_ms = synthesis.latency_ms
        trace.model_used = synthesis.model
        trace.prompt_version = synthesis.prompt_version
        trace.prompt_tokens = synthesis.prompt_tokens
        trace.completion_tokens = synthesis.completion_tokens
        trace.verification = synthesis.verification
        trace.confidence = synthesis.confidence.as_dict()
        trace.answer = synthesis.answer
        trace.citations = tuple(
            {
                "municipality": citation.municipality,
                "bylaw": citation.bylaw_title,
                "bylaw_number": citation.bylaw_number,
                "section": citation.section,
                "page": citation.page,
                "amendment_status": citation.amendment_status,
                "quote": citation.quote,
            }
            for citation in synthesis.citations
        )

        # An abstention produced by verification is a distinct outcome from one
        # produced by empty retrieval — the difference matters when debugging.
        outcome = AnswerOutcome.UNVERIFIED if synthesis.abstained else AnswerOutcome.ANSWERED
        trace.outcome = outcome

        return await self._finish(
            trace,
            started,
            AnswerResult(
                outcome=outcome,
                answer=synthesis.answer,
                citations=synthesis.citations,
                confidence=synthesis.confidence,
                conditions=synthesis.conditions,
                conflicts=conflicts,
                trace=trace,
            ),
        )

    # -- failure paths -------------------------------------------------------

    @staticmethod
    def _simple(outcome: AnswerOutcome, trace: PipelineTrace) -> AnswerResult:
        trace.outcome = outcome
        trace.answer = _MESSAGES[outcome]
        return AnswerResult(outcome=outcome, answer=_MESSAGES[outcome], trace=trace)

    def _clarify(self, plan: QueryPlan, trace: PipelineTrace) -> AnswerResult:
        """Ask rather than guess.

        City and Township of Langley have separate bylaws. Answering from either
        without asking produces a wrong answer that looks entirely plausible.
        """
        prompt = plan.clarification_prompt() or "Which municipality did you mean?"
        options = tuple(
            record.official_name
            for name in plan.ambiguous_names
            for record in self.router.registry.candidates(name)
        )

        trace.outcome = AnswerOutcome.NEEDS_CLARIFICATION
        trace.answer = prompt

        return AnswerResult(
            outcome=AnswerOutcome.NEEDS_CLARIFICATION,
            answer=prompt,
            clarification=prompt,
            clarification_options=options,
            trace=trace,
        )

    async def _nothing_found(
        self, plan: QueryPlan, filters: RetrievalFilters, trace: PipelineTrace
    ) -> AnswerResult:
        """Distinguish "no such rule" from "the rule exists but is superseded".

        These look identical from the user's side and mean opposite things. The
        second is worth saying out loud, because it tells them a rule exists and
        the current version is simply not indexed.
        """
        if not self.detect_outdated or not filters.in_force_only:
            return self._simple(AnswerOutcome.NO_RELEVANT_BYLAW, trace)

        try:
            stale_chunks, _ = await self.retriever.retrieve(
                plan.query,
                filters=RetrievalFilters(
                    municipality_slugs=filters.municipality_slugs,
                    in_force_only=False,
                    document_ids=filters.document_ids,
                    chunk_types=filters.chunk_types,
                ),
            )
        except Exception:
            return self._simple(AnswerOutcome.NO_RELEVANT_BYLAW, trace)

        outdated = tuple(
            dict.fromkeys(
                f"{chunk.document_title or 'Untitled'} ({chunk.document_status.value})"
                for chunk in stale_chunks
                if chunk.document_status is not DocumentStatus.IN_FORCE
            )
        )
        if not outdated:
            return self._simple(AnswerOutcome.NO_RELEVANT_BYLAW, trace)

        message = (
            "I found bylaw text that addresses this, but only in documents that "
            "are superseded or repealed, so I will not answer from them. The "
            "current version may not be indexed. Contact the municipality to "
            "confirm the rule in force.\n\nDocuments found: " + "; ".join(outdated[:5])
        )

        trace.outcome = AnswerOutcome.ONLY_OUTDATED
        trace.answer = message

        return AnswerResult(
            outcome=AnswerOutcome.ONLY_OUTDATED,
            answer=message,
            outdated_documents=outdated,
            trace=trace,
        )

    def _conflicted(
        self,
        conflicts: Sequence[ConflictDetail],
        chunks: Sequence[RetrievedChunk],
        trace: PipelineTrace,
    ) -> AnswerResult:
        lines = [
            "Two or more in-force bylaw documents appear to regulate this "
            "differently, so I will not choose between them. Review both and "
            "confirm with the municipality:",
            "",
        ]
        lines.extend(f"  - {conflict.detail}" for conflict in conflicts[:5])

        message = "\n".join(lines)
        trace.outcome = AnswerOutcome.CONFLICTING_AMENDMENTS
        trace.answer = message

        return AnswerResult(
            outcome=AnswerOutcome.CONFLICTING_AMENDMENTS,
            answer=message,
            conflicts=tuple(conflicts),
            trace=trace,
        )

    def _generation_down(
        self, chunks: Sequence[RetrievedChunk], trace: PipelineTrace
    ) -> AnswerResult:
        """Generation is down, but retrieval worked.

        The retrieved sections are returned as citations so the user can read
        the source text directly. Degraded, not useless.
        """
        message = _MESSAGES[AnswerOutcome.GENERATION_UNAVAILABLE]
        citations = tuple(
            SynthesizedCitation(
                municipality=chunk.municipality_name,
                bylaw_title=chunk.document_title,
                bylaw_number=chunk.bylaw_number,
                section=chunk.section_number,
                section_path=chunk.section_path,
                page=chunk.page_number,
                quote=chunk.body[:400],
                amendment_status=chunk.document_status.value,
                document_id=chunk.document_id,
                chunk_id=chunk.chunk_id,
                from_ocr=chunk.from_ocr,
            )
            for chunk in chunks[:5]
        )

        trace.outcome = AnswerOutcome.GENERATION_UNAVAILABLE
        trace.answer = message

        return AnswerResult(
            outcome=AnswerOutcome.GENERATION_UNAVAILABLE,
            answer=message,
            citations=citations,
            trace=trace,
        )

    # -- analysis ------------------------------------------------------------

    @staticmethod
    def _detect_conflicts(chunks: Sequence[RetrievedChunk]) -> tuple[ConflictDetail, ...]:
        """Find in-force documents regulating the same section differently.

        Only in-force documents are compared. A base bylaw and its consolidation
        both appearing is normal and already handled by lineage resolution; two
        *current* documents disagreeing is not, and is worth stopping for.
        """
        groups: dict[tuple[str | None, str | None], list[RetrievedChunk]] = {}
        for chunk in chunks:
            if chunk.document_status is not DocumentStatus.IN_FORCE:
                continue
            if not chunk.section_number:
                continue
            groups.setdefault((chunk.municipality_slug, chunk.section_number), []).append(chunk)

        conflicts: list[ConflictDetail] = []
        for (municipality, section), members in groups.items():
            documents = {chunk.document_id for chunk in members}
            if len(documents) < 2:
                continue

            titles = tuple(
                dict.fromkeys(chunk.document_title or chunk.document_id for chunk in members)
            )
            where = municipality or "an unidentified municipality"
            detail = (
                f"Section {section} in {where} appears in {len(documents)} "
                f"in-force documents: {', '.join(titles)}"
            )
            conflicts.append(
                ConflictDetail(
                    municipality=municipality,
                    section=section,
                    documents=titles,
                    detail=detail,
                )
            )

        return tuple(conflicts)

    @staticmethod
    def _filters_for(plan: QueryPlan) -> RetrievalFilters:
        return RetrievalFilters(
            municipality_slugs=plan.municipality_slugs,
            in_force_only=True,
        )

    @staticmethod
    def _record_retrieval(
        trace: PipelineTrace,
        chunks: Sequence[RetrievedChunk],
        retrieval_trace: RetrievalTrace,
    ) -> None:
        trace.collection = retrieval_trace.collection
        trace.retrieved_chunk_ids = tuple(chunk.chunk_id for chunk in chunks)
        trace.retrieval_scores = tuple(chunk.provenance() for chunk in chunks)
        trace.dense_candidates = retrieval_trace.dense_candidates
        trace.sparse_candidates = retrieval_trace.sparse_candidates
        trace.fused_candidates = retrieval_trace.fused_candidates
        trace.reranked = retrieval_trace.reranked
        trace.filters = retrieval_trace.filters
        trace.retrieval_ms = retrieval_trace.duration_ms

    # -- completion ----------------------------------------------------------

    async def _finish(
        self, trace: PipelineTrace, started: float, result: AnswerResult
    ) -> AnswerResult:
        trace.total_ms = int((time.perf_counter() - started) * 1000)

        if self.trace_sink is not None:
            try:
                await self.trace_sink.record(trace)
            except Exception as exc:
                logger.warning("trace_persist_failed", error=str(exc))

        logger.info(
            "answer_completed",
            trace_id=trace.trace_id,
            outcome=trace.outcome.value,
            chunks=len(trace.retrieved_chunk_ids),
            citations=len(trace.citations),
            confidence=trace.confidence.get("score"),
            total_ms=trace.total_ms,
        )
        return result
