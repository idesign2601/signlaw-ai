"""End-to-end pipeline behaviour, driven with fakes.

No Postgres, no Ollama, no model weights. The retriever and LLM are stubs, so
the orchestration — routing, failure handling, tracing — is tested in
milliseconds and every failure mode can be provoked deliberately.

The five failure modes each get their own class, because each needs a
*different* response and conflating them is how a system ends up answering
"no such rule" when the truth is "the rule exists but the version I have is
repealed".
"""

from __future__ import annotations

import json

import pytest

from app.adapters.llm.base import GenerationResult
from app.core.exceptions import ExternalServiceError, IndexNotReadyError, LLMError
from app.db.enums import ChunkType, ConfidenceBand, DocumentStatus
from app.rag.results import RetrievalTrace, RetrievedChunk
from app.rag.retriever import RetrievalFilters
from app.rag.synthesizer import AnswerSynthesizer
from app.services.rag_service import AnswerOutcome, RagService

BODY = (
    "5.3 Fascia Signs\n"
    "(a) A fascia sign must not exceed twenty percent (20%) of the area of the "
    "building face to which it is attached."
)


def chunk(
    chunk_id: str = "c1",
    *,
    body: str = BODY,
    document_id: str = "doc-1",
    section: str | None = "5.3",
    status: DocumentStatus = DocumentStatus.IN_FORCE,
    municipality: str | None = "burnaby",
    title: str | None = "Sign Bylaw No. 13743",
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        body=body,
        chunk_type=ChunkType.PROSE,
        document_id=document_id,
        document_title=title,
        municipality_slug=municipality,
        municipality_name=municipality.title() if municipality else None,
        bylaw_number="13743",
        section_number=section,
        section_path=f"Part 5 > {section}" if section else None,
        section_heading="Fascia Signs",
        page_number=22,
        document_status=status,
        fused_score=0.9,
    )


class FakeRetriever:
    """Returns a fixed result set, with a separate set for the no-filter probe."""

    def __init__(
        self,
        chunks: list[RetrievedChunk] | None = None,
        *,
        unfiltered: list[RetrievedChunk] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.chunks = chunks or []
        self.unfiltered = unfiltered
        self.raises = raises
        self.calls: list[RetrievalFilters | None] = []

    async def retrieve(self, query, *, filters=None, top_n=None):
        self.calls.append(filters)
        if self.raises is not None:
            raise self.raises

        # The service re-queries without the in-force filter to distinguish
        # "no such rule" from "only superseded text exists".
        if filters is not None and not filters.in_force_only and self.unfiltered is not None:
            payload = self.unfiltered
        else:
            payload = self.chunks

        trace = RetrievalTrace(
            query=query,
            collection="signlaw_bge_m3_v1",
            filters=filters.as_dict() if filters else {},
            dense_candidates=len(payload),
            sparse_candidates=len(payload),
            fused_candidates=len(payload),
            returned=len(payload),
            reranked=False,
            duration_ms=5,
            chunks=tuple(c.provenance() for c in payload),
        )
        return list(payload), trace


class FakeLLM:
    """Returns a canned JSON answer, or raises."""

    def __init__(self, payload: dict | None = None, raises: Exception | None = None) -> None:
        self.payload = (
            payload
            if payload is not None
            else {
                "answer": "A fascia sign must not exceed 20% of the building face [S1].",
                "citations": [
                    {
                        "source_id": 1,
                        "quote": "must not exceed twenty percent (20%)",
                        "supports": "the 20% limit",
                    }
                ],
                "answered": True,
            }
        )
        self.raises = raises
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "qwen2.5:14b-instruct"

    @property
    def supports_schema(self) -> bool:
        return True

    async def generate(self, messages, *, schema=None, temperature=None, max_tokens=None):
        if self.raises is not None:
            raise self.raises
        self.prompts.append(messages[-1].content)
        return GenerationResult(
            text=json.dumps(self.payload),
            model=self.model,
            prompt_tokens=800,
            completion_tokens=120,
            latency_ms=900,
            finish_reason="stop",
        )

    async def health(self):
        return True, "ready"


def build(retriever: FakeRetriever, llm: FakeLLM | None = None, **kwargs) -> RagService:
    return RagService(
        retriever=retriever,
        synthesizer=AnswerSynthesizer(llm=llm or FakeLLM()),
        **kwargs,
    )


# -----------------------------------------------------------------------------


class TestHappyPath:
    async def test_answers_with_citations(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("What is the maximum fascia sign area in Burnaby?")

        assert result.outcome is AnswerOutcome.ANSWERED
        assert result.answered
        assert len(result.citations) == 1

    async def test_citation_carries_every_required_field(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("Maximum fascia sign area in Burnaby?")
        citation = result.citations[0]

        assert citation.municipality == "Burnaby"
        assert citation.bylaw_title == "Sign Bylaw No. 13743"
        assert citation.section == "5.3"
        assert citation.page == 22
        assert citation.amendment_status == "in_force"

    async def test_confidence_is_scored(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("Maximum fascia sign area in Burnaby?")

        assert result.confidence is not None
        assert 0.0 < result.confidence_score <= 1.0
        assert result.band is not ConfidenceBand.INSUFFICIENT

    async def test_retrieval_is_scoped_to_the_named_city(self) -> None:
        retriever = FakeRetriever([chunk()])
        await build(retriever).answer("Fascia signs in Burnaby?")

        assert retriever.calls[0] is not None
        assert retriever.calls[0].municipality_slugs == ("burnaby",)
        assert retriever.calls[0].in_force_only


class TestTracing:
    async def test_trace_captures_every_required_field(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("Fascia sign area in Burnaby?")
        trace = result.trace

        assert trace is not None
        assert trace.question
        assert trace.retrieved_chunk_ids == ("c1",)
        assert trace.retrieval_scores
        assert trace.model_used == "qwen2.5:14b-instruct"
        assert trace.prompt_version
        assert trace.answer
        assert trace.citations
        assert trace.verification
        assert trace.confidence
        assert trace.total_ms >= 0

    async def test_trace_serialises(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("Fascia signs in Burnaby?")
        payload = result.trace.as_dict()  # type: ignore[union-attr]

        assert set(payload) >= {
            "trace_id",
            "question",
            "routing",
            "retrieval",
            "generation",
            "outcome",
            "citations",
            "verification",
            "confidence",
        }
        json.dumps(payload)  # must be JSON-serialisable for persistence

    async def test_trace_records_token_usage(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("Fascia signs in Burnaby?")

        assert result.trace.prompt_tokens == 800  # type: ignore[union-attr]
        assert result.trace.completion_tokens == 120  # type: ignore[union-attr]

    async def test_sink_receives_the_trace(self) -> None:
        recorded = []

        class Sink:
            async def record(self, trace):
                recorded.append(trace)

        service = build(FakeRetriever([chunk()]), trace_sink=Sink())
        await service.answer("Fascia signs in Burnaby?")
        assert len(recorded) == 1

    async def test_a_failing_sink_does_not_fail_the_answer(self) -> None:
        class BadSink:
            async def record(self, trace):
                raise RuntimeError("disk full")

        service = build(FakeRetriever([chunk()]), trace_sink=BadSink())
        result = await service.answer("Fascia signs in Burnaby?")
        assert result.answered


class TestNoRelevantBylaw:
    async def test_empty_retrieval_declines(self) -> None:
        service = build(FakeRetriever([]))
        result = await service.answer("Fascia sign rules in Burnaby?")

        assert result.outcome is AnswerOutcome.NO_RELEVANT_BYLAW
        assert not result.citations

    async def test_message_does_not_claim_the_bylaw_is_silent(self) -> None:
        # Absence of evidence is not evidence of permission.
        service = build(FakeRetriever([]))
        result = await service.answer("Fascia sign rules in Burnaby?")
        assert "may mean" in result.answer

    async def test_generation_is_never_attempted(self) -> None:
        llm = FakeLLM()
        service = build(FakeRetriever([]), llm)
        await service.answer("Fascia sign rules in Burnaby?")
        assert llm.prompts == []


class TestOnlyOutdated:
    async def test_detects_superseded_only_results(self) -> None:
        # In-force search finds nothing; the unfiltered probe finds superseded
        # text. These mean opposite things and must be reported differently.
        retriever = FakeRetriever([], unfiltered=[chunk(status=DocumentStatus.SUPERSEDED)])
        result = await build(retriever).answer("Fascia sign rules in Burnaby?")

        assert result.outcome is AnswerOutcome.ONLY_OUTDATED
        assert result.outdated_documents

    async def test_names_the_outdated_documents(self) -> None:
        retriever = FakeRetriever([], unfiltered=[chunk(status=DocumentStatus.REPEALED)])
        result = await build(retriever).answer("Fascia sign rules in Burnaby?")
        assert "Sign Bylaw No. 13743" in result.outdated_documents[0]

    async def test_does_not_answer_from_outdated_text(self) -> None:
        llm = FakeLLM()
        retriever = FakeRetriever([], unfiltered=[chunk(status=DocumentStatus.SUPERSEDED)])
        await build(retriever, llm).answer("Fascia sign rules in Burnaby?")
        assert llm.prompts == []

    async def test_probe_can_be_disabled(self) -> None:
        retriever = FakeRetriever([], unfiltered=[chunk(status=DocumentStatus.SUPERSEDED)])
        service = build(retriever, detect_outdated=False)
        result = await service.answer("Fascia sign rules in Burnaby?")
        assert result.outcome is AnswerOutcome.NO_RELEVANT_BYLAW


class TestUnclearMunicipality:
    async def test_bare_langley_asks_rather_than_guesses(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("What are the sign rules in Langley?")

        assert result.outcome is AnswerOutcome.NEEDS_CLARIFICATION
        assert result.clarification

    async def test_both_options_are_offered(self) -> None:
        service = build(FakeRetriever([chunk()]))
        result = await service.answer("Sign rules in Langley?")

        assert len(result.clarification_options) == 2
        assert any("City of Langley" in option for option in result.clarification_options)
        assert any("Township of Langley" in option for option in result.clarification_options)

    async def test_nothing_is_retrieved_or_generated(self) -> None:
        retriever = FakeRetriever([chunk()])
        llm = FakeLLM()
        await build(retriever, llm).answer("Sign rules in Langley?")

        assert retriever.calls == []
        assert llm.prompts == []

    async def test_qualified_name_proceeds(self) -> None:
        service = build(FakeRetriever([chunk(municipality="langley-township")]))
        result = await service.answer("Sign rules in the Township of Langley?")
        assert result.outcome is AnswerOutcome.ANSWERED


class TestConflictingAmendments:
    async def test_two_in_force_documents_on_one_section_abstains(self) -> None:
        service = build(
            FakeRetriever(
                [
                    chunk("c1", document_id="doc-1", title="Sign Bylaw No. 13743"),
                    chunk("c2", document_id="doc-2", title="Sign Bylaw No. 14000"),
                ]
            )
        )
        result = await service.answer("Fascia sign area in Burnaby?")

        assert result.outcome is AnswerOutcome.CONFLICTING_AMENDMENTS
        assert result.conflicts

    async def test_both_documents_are_named(self) -> None:
        service = build(
            FakeRetriever(
                [
                    chunk("c1", document_id="doc-1", title="Sign Bylaw No. 13743"),
                    chunk("c2", document_id="doc-2", title="Sign Bylaw No. 14000"),
                ]
            )
        )
        result = await service.answer("Fascia sign area in Burnaby?")
        assert "13743" in result.answer and "14000" in result.answer

    async def test_superseded_documents_do_not_count_as_conflicts(self) -> None:
        # A base bylaw plus its consolidation is normal, not a conflict.
        service = build(
            FakeRetriever(
                [
                    chunk("c1", document_id="doc-1"),
                    chunk(
                        "c2",
                        document_id="doc-2",
                        status=DocumentStatus.SUPERSEDED,
                    ),
                ]
            )
        )
        result = await service.answer("Fascia sign area in Burnaby?")
        assert result.outcome is AnswerOutcome.ANSWERED

    async def test_different_sections_are_not_conflicts(self) -> None:
        service = build(
            FakeRetriever(
                [
                    chunk("c1", document_id="doc-1", section="5.3"),
                    chunk("c2", document_id="doc-2", section="5.4"),
                ]
            )
        )
        result = await service.answer("Fascia sign area in Burnaby?")
        assert result.outcome is AnswerOutcome.ANSWERED

    async def test_can_be_configured_to_answer_anyway(self) -> None:
        service = build(
            FakeRetriever(
                [
                    chunk("c1", document_id="doc-1"),
                    chunk("c2", document_id="doc-2"),
                ]
            ),
            abstain_on_conflict=False,
        )
        result = await service.answer("Fascia sign area in Burnaby?")
        assert result.outcome is AnswerOutcome.ANSWERED
        assert result.conflicts


class TestGenerationUnavailable:
    async def test_ollama_down_degrades_rather_than_fails(self) -> None:
        service = build(
            FakeRetriever([chunk()]),
            FakeLLM(raises=ExternalServiceError("ollama", "connection refused")),
        )
        result = await service.answer("Fascia sign area in Burnaby?")

        assert result.outcome is AnswerOutcome.GENERATION_UNAVAILABLE
        assert not result.answered

    async def test_retrieved_sections_are_still_returned(self) -> None:
        # Retrieval worked, so the user can read the source text directly.
        service = build(
            FakeRetriever([chunk()]),
            FakeLLM(raises=LLMError("model not pulled")),
        )
        result = await service.answer("Fascia sign area in Burnaby?")

        assert result.citations
        assert result.citations[0].section == "5.3"

    async def test_it_is_reported_as_infrastructure(self) -> None:
        service = build(FakeRetriever([chunk()]), FakeLLM(raises=LLMError("down")))
        result = await service.answer("Fascia sign area in Burnaby?")
        assert result.outcome.is_infrastructure_failure


class TestIndexNotReady:
    async def test_empty_index_is_reported_distinctly(self) -> None:
        service = build(FakeRetriever(raises=IndexNotReadyError()))
        result = await service.answer("Fascia sign area in Burnaby?")

        assert result.outcome is AnswerOutcome.INDEX_NOT_READY
        assert result.outcome.is_infrastructure_failure


class TestOutOfScope:
    @pytest.mark.parametrize(
        "question",
        ["What is the weather in Vancouver?", "What is the population of Surrey?"],
    )
    async def test_declines_without_retrieving(self, question: str) -> None:
        retriever = FakeRetriever([chunk()])
        llm = FakeLLM()
        result = await build(retriever, llm).answer(question)

        assert result.outcome is AnswerOutcome.OUT_OF_SCOPE
        assert retriever.calls == []
        assert llm.prompts == []


class TestVerificationFailure:
    async def test_fabricated_number_forces_abstention(self) -> None:
        # The model cites a real excerpt but states a value found nowhere in it.
        llm = FakeLLM(
            {
                "answer": "A fascia sign must not exceed 6.5 metres in height [S1].",
                "citations": [
                    {
                        "source_id": 1,
                        "quote": "must not exceed twenty percent (20%)",
                        "supports": "height limit",
                    }
                ],
                "answered": True,
            }
        )
        result = await build(FakeRetriever([chunk()]), llm).answer("Fascia sign height in Burnaby?")

        assert result.outcome is AnswerOutcome.UNVERIFIED
        assert not result.citations

    async def test_citation_to_a_nonexistent_source_abstains(self) -> None:
        llm = FakeLLM(
            {
                "answer": "Projecting signs are prohibited [S9].",
                "citations": [{"source_id": 9, "quote": "prohibited", "supports": "ban"}],
                "answered": True,
            }
        )
        result = await build(FakeRetriever([chunk()]), llm).answer(
            "Are projecting signs allowed in Burnaby?"
        )
        assert result.outcome is AnswerOutcome.UNVERIFIED

    async def test_model_reporting_no_answer_is_respected(self) -> None:
        llm = FakeLLM(
            {"answer": "The excerpts do not cover this.", "citations": [], "answered": False}
        )
        result = await build(FakeRetriever([chunk()]), llm).answer("Holographic signs in Burnaby?")
        assert result.outcome is AnswerOutcome.UNVERIFIED
