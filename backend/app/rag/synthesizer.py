"""Answer synthesis.

Ties generation to enforcement. The model produces a structured answer under a
JSON schema; the verifier checks it against the retrieved excerpts; the scorer
grades what survived. An answer that fails verification is replaced with an
abstention rather than shown with a caveat, because a caveated fabrication is
still a fabrication with a citation attached.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.adapters.llm.base import ChatMessage, LLMProviderProtocol
from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.db.enums import ConfidenceBand
from app.domain.confidence import ConfidenceReport, ConfidenceScorer
from app.rag.prompts import ANSWER_SCHEMA, PROMPT_VERSION, build_answer_prompt
from app.rag.results import RetrievedChunk
from app.rag.verifier import CitationClaim, CitationVerifier, VerificationReport

__all__ = ["AnswerSynthesizer", "SynthesisResult", "SynthesizedCitation"]

logger = get_logger(__name__)

ABSTENTION_TEXT = (
    "The indexed bylaws do not contain enough information to answer this "
    "reliably. Contact the municipality's planning department to confirm."
)


def _string_tuple(value: object) -> tuple[str, ...]:
    """Coerce a parsed JSON field to a tuple of strings.

    The model's output is untrusted: a field the schema declares as an array
    may arrive as a string, a number, or absent entirely.
    """
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value.strip():
        return (value,)
    return ()


@dataclass(frozen=True, slots=True)
class SynthesizedCitation:
    """A verified citation, in the form the API returns."""

    municipality: str | None
    bylaw_title: str | None
    bylaw_number: str | None
    section: str | None
    section_path: str | None
    page: int
    quote: str
    amendment_status: str
    consolidation_date: str | None = None
    last_amendment_date: str | None = None
    document_id: str = ""
    chunk_id: str = ""
    from_ocr: bool = False

    @property
    def deep_link(self) -> str:
        return f"/documents/{self.document_id}/page/{self.page}"

    def render(self) -> str:
        parts = [part for part in (self.municipality, self.bylaw_title) if part]
        head = " — ".join(parts) if parts else "Unattributed document"
        tail = [f"s. {self.section}"] if self.section else []
        tail.append(f"p. {self.page}")
        return f"{head}, {', '.join(tail)}"


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    """The complete answer, its evidence, and how much to trust it."""

    answer: str
    citations: tuple[SynthesizedCitation, ...]
    confidence: ConfidenceReport
    abstained: bool
    conditions: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    latency_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    verification: dict[str, object] = field(default_factory=dict)

    @property
    def band(self) -> ConfidenceBand:
        return self.confidence.band


@dataclass
class AnswerSynthesizer:
    """Generates, verifies and scores an answer."""

    llm: LLMProviderProtocol
    verifier: CitationVerifier = field(default_factory=CitationVerifier)
    scorer: ConfidenceScorer = field(default_factory=ConfidenceScorer)

    async def synthesize(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        *,
        is_comparison: bool = False,
        municipality_resolved: bool = True,
    ) -> SynthesisResult:
        """Produce a verified answer for a question."""
        started = time.perf_counter()

        if not chunks:
            return self._abstain(
                "no bylaw excerpts were retrieved for this question",
                municipality_resolved=municipality_resolved,
                started=started,
            )

        system, user, context = build_answer_prompt(
            question, chunks, is_comparison=is_comparison
        )

        try:
            generation = await self.llm.generate(
                [ChatMessage("system", system), ChatMessage("user", user)],
                schema=ANSWER_SCHEMA if self.llm.supports_schema else None,
            )
        except LLMError as exc:
            logger.error("synthesis_failed", error=exc.message)
            raise

        if generation.was_truncated:
            # A truncated answer may have lost its citation block entirely,
            # which makes it unverifiable rather than merely incomplete.
            return self._abstain(
                "generation hit the token limit before completing its citations",
                municipality_resolved=municipality_resolved,
                started=started,
                model=generation.model,
            )

        parsed = self._parse(generation.text)
        if parsed is None:
            return self._abstain(
                "the model did not return a parseable answer",
                municipality_resolved=municipality_resolved,
                started=started,
                model=generation.model,
            )

        answer_text = str(parsed.get("answer", "")).strip()
        model_answered = bool(parsed.get("answered", True))

        claims = self._extract_claims(parsed)
        report = self.verifier.verify(
            answer_text, claims, chunks, source_map=context.source_map
        )

        if not model_answered or report.should_abstain:
            reason = (
                report.abstain_reason
                or "the model reported that the excerpts do not answer the question"
            )
            return self._abstain(
                reason,
                municipality_resolved=municipality_resolved,
                started=started,
                model=generation.model,
                verification=report.as_dict(),
            )

        citations = self._build_citations(report, chunks)
        confidence = self.scorer.score(
            chunks=chunks,
            cited_chunk_ids=report.cited_chunk_ids,
            citation_precision=report.citation_precision,
            uncited_claim_count=len(report.uncited_claims),
            municipality_resolved=municipality_resolved,
            has_conflicts=bool(parsed.get("conflicts")),
        )

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "answer_synthesized",
            model=generation.model,
            citations=len(citations),
            citation_precision=round(report.citation_precision, 3),
            confidence=confidence.score,
            band=confidence.band.value,
            latency_ms=latency_ms,
        )

        return SynthesisResult(
            answer=answer_text,
            citations=citations,
            confidence=confidence,
            abstained=False,
            conditions=_string_tuple(parsed.get("conditions")),
            conflicts=_string_tuple(parsed.get("conflicts")),
            model=generation.model,
            latency_ms=latency_ms,
            prompt_tokens=generation.prompt_tokens,
            completion_tokens=generation.completion_tokens,
            verification=report.as_dict(),
        )

    # -- parsing -------------------------------------------------------------

    @staticmethod
    def _parse(text: str) -> dict[str, object] | None:
        """Parse the model's JSON, tolerating a stray code fence.

        Schema-constrained decoding makes this reliable, but the fallback costs
        nothing and covers providers that ignore the constraint.
        """
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
            candidate = candidate.strip()

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start == -1 or end <= start:
                return None
            try:
                parsed = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                return None

        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _extract_claims(parsed: dict[str, object]) -> list[CitationClaim]:
        raw = parsed.get("citations")
        if not isinstance(raw, list):
            return []

        claims: list[CitationClaim] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                source_id = int(entry.get("source_id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            claims.append(
                CitationClaim(
                    source_id=source_id,
                    quote=str(entry.get("quote") or ""),
                    supports=str(entry.get("supports") or ""),
                )
            )
        return claims

    @staticmethod
    def _build_citations(
        report: VerificationReport, chunks: Sequence[RetrievedChunk]
    ) -> tuple[SynthesizedCitation, ...]:
        """Build the returned citations from verified claims only."""
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        citations: list[SynthesizedCitation] = []
        seen: set[tuple[str, str]] = set()

        for claim in report.valid_claims:
            chunk = by_id.get(claim.chunk_id or "")
            if chunk is None:
                continue

            key = (chunk.chunk_id, claim.quote[:80])
            if key in seen:
                continue
            seen.add(key)

            citations.append(
                SynthesizedCitation(
                    municipality=chunk.municipality_name,
                    bylaw_title=chunk.document_title,
                    bylaw_number=chunk.bylaw_number,
                    section=chunk.section_number,
                    section_path=chunk.section_path,
                    page=chunk.page_number,
                    quote=claim.quote.strip(),
                    amendment_status=chunk.document_status.value,
                    consolidation_date=(
                        chunk.consolidation_date.isoformat()
                        if chunk.consolidation_date
                        else None
                    ),
                    last_amendment_date=(
                        chunk.last_amendment_date.isoformat()
                        if chunk.last_amendment_date
                        else None
                    ),
                    document_id=chunk.document_id,
                    chunk_id=chunk.chunk_id,
                    from_ocr=chunk.from_ocr,
                )
            )
        return tuple(citations)

    # -- abstention ----------------------------------------------------------

    def _abstain(
        self,
        reason: str,
        *,
        municipality_resolved: bool,
        started: float,
        model: str = "",
        verification: dict[str, object] | None = None,
    ) -> SynthesisResult:
        logger.info("answer_abstained", reason=reason)

        confidence = self.scorer.score(
            chunks=(),
            cited_chunk_ids=(),
            citation_precision=0.0,
            uncited_claim_count=0,
            municipality_resolved=municipality_resolved,
            abstained=True,
        )

        return SynthesisResult(
            answer=ABSTENTION_TEXT,
            citations=(),
            confidence=confidence,
            abstained=True,
            model=model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            verification={**(verification or {}), "abstain_reason": reason},
        )
