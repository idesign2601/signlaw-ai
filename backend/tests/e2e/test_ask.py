"""Ask endpoint behaviour.

The interesting cases are the abstentions. An answer is easy to serialise; what
matters is that each of the pipeline's ways of declining survives the HTTP
boundary intact, because the interface has to treat them differently and a
caller that cannot tell them apart will render "the model is down" as "the
bylaw is silent".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.api.deps import get_rag_service
from app.db.enums import ConfidenceBand
from app.domain.confidence import ConfidenceReport
from app.rag.retriever import RetrievalFilters
from app.rag.synthesizer import SynthesizedCitation
from app.services.rag_service import AnswerOutcome, AnswerResult


@dataclass
class _StubService:
    """Returns a scripted result and records the filters it was handed."""

    result: AnswerResult
    calls: list[RetrievalFilters | None] = field(default_factory=list)

    async def answer(
        self,
        question: str,
        *,
        filters: RetrievalFilters | None = None,
        top_n: int | None = None,
    ) -> AnswerResult:
        self.calls.append(filters)
        return self.result


def _answered() -> AnswerResult:
    return AnswerResult(
        outcome=AnswerOutcome.ANSWERED,
        answer="Channel letters are permitted as a fascia sign.",
        citations=(
            SynthesizedCitation(
                municipality="City of Vancouver",
                bylaw_title="Sign By-law",
                bylaw_number="11879",
                section="4.2.1",
                section_path="Part 4 > 4.2 > 4.2.1",
                page=17,
                quote="Fascia signs may consist of individual channel letters.",
                amendment_status="in_force",
                document_id="doc-1",
                chunk_id="chunk-1",
            ),
        ),
        confidence=ConfidenceReport(
            score=0.82,
            band=ConfidenceBand.HIGH,
            factors=(),
            explanation="Every statement is cited to in-force text.",
        ),
    )


def _install(app: FastAPI, result: AnswerResult) -> _StubService:
    service = _StubService(result=result)
    app.dependency_overrides[get_rag_service] = lambda: service
    return service


class TestAnswering:
    async def test_returns_answer_with_citation_detail(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        _install(app, _answered())

        response = await client.post(
            "/api/v1/ask", json={"question": "Are channel letters allowed?"}
        )
        assert response.status_code == 200

        payload = response.json()
        assert payload["answered"] is True
        assert payload["outcome"] == "answered"
        assert payload["confidence"]["band"] == "high"

        citation = payload["citations"][0]
        # Everything a reader needs to check the claim against the PDF.
        assert citation["municipality"] == "City of Vancouver"
        assert citation["bylaw_number"] == "11879"
        assert citation["section"] == "4.2.1"
        assert citation["page"] == 17

    async def test_municipality_slug_scopes_retrieval(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        service = _install(app, _answered())

        await client.post(
            "/api/v1/ask",
            json={"question": "Are channel letters allowed?", "municipality": "burnaby"},
        )

        filters = service.calls[0]
        assert filters is not None
        assert filters.municipality_slugs == ("burnaby",)

    async def test_repealed_text_is_never_opted_into(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """in_force_only is not a caller-supplied preference."""
        service = _install(app, _answered())

        await client.post(
            "/api/v1/ask",
            json={
                "question": "Are channel letters allowed?",
                "municipality": "surrey",
                "in_force_only": False,  # ignored: not part of the contract
            },
        )

        assert service.calls[0].in_force_only is True

    async def test_unknown_municipality_is_rejected(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        _install(app, _answered())

        response = await client.post(
            "/api/v1/ask",
            json={"question": "Are channel letters allowed?", "municipality": "atlantis"},
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("question", ["", "hi"])
    async def test_trivial_questions_are_rejected(
        self, app: FastAPI, client: AsyncClient, question: str
    ) -> None:
        _install(app, _answered())

        response = await client.post("/api/v1/ask", json={"question": question})
        assert response.status_code == 422


class TestAbstentions:
    async def test_ambiguous_municipality_returns_both_options(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """The Langley case, end to end.

        A 200 with answered=False and two options — not a guess, and not an
        error either. The interface turns this into a follow-up question.
        """
        _install(
            app,
            AnswerResult(
                outcome=AnswerOutcome.NEEDS_CLARIFICATION,
                answer="Which Langley did you mean?",
                clarification="Which Langley did you mean?",
                clarification_options=("City of Langley", "Township of Langley"),
            ),
        )

        response = await client.post(
            "/api/v1/ask", json={"question": "What are the sign rules in Langley?"}
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["answered"] is False
        assert payload["outcome"] == "needs_clarification"
        assert payload["clarification_options"] == [
            "City of Langley",
            "Township of Langley",
        ]

    async def test_only_outdated_text_is_reported_as_such(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        """Distinct from 'nothing found', and the difference matters.

        It tells the user a rule exists and the current version simply is not
        indexed — which is actionable, where 'no relevant bylaw' is not.
        """
        _install(
            app,
            AnswerResult(
                outcome=AnswerOutcome.ONLY_OUTDATED,
                answer="Found only superseded text.",
                outdated_documents=("Sign Bylaw 1972 (repealed)",),
            ),
        )

        payload = (await client.post("/api/v1/ask", json={"question": "Fascia sign area?"})).json()

        assert payload["outcome"] == "only_outdated"
        assert payload["outdated_documents"] == ["Sign Bylaw 1972 (repealed)"]
        assert payload["citations"] == []

    async def test_no_relevant_bylaw_is_a_success_not_an_error(
        self, app: FastAPI, client: AsyncClient
    ) -> None:
        _install(
            app,
            AnswerResult(
                outcome=AnswerOutcome.NO_RELEVANT_BYLAW,
                answer="I could not find anything addressing this.",
            ),
        )

        response = await client.post(
            "/api/v1/ask", json={"question": "Rules for holographic signs?"}
        )

        assert response.status_code == 200
        assert response.json()["answered"] is False


class TestInfrastructureFailures:
    @pytest.mark.parametrize(
        "outcome",
        [AnswerOutcome.GENERATION_UNAVAILABLE, AnswerOutcome.INDEX_NOT_READY],
    )
    async def test_infrastructure_failure_is_5xx(
        self, app: FastAPI, client: AsyncClient, outcome: AnswerOutcome
    ) -> None:
        """Our fault, not a limit of the corpus.

        A caller should retry, and must not tell the user the bylaw is silent.
        """
        _install(app, AnswerResult(outcome=outcome, answer="Unavailable."))

        response = await client.post(
            "/api/v1/ask", json={"question": "Are channel letters allowed?"}
        )
        assert response.status_code == 503
