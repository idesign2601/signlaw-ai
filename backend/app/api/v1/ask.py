"""Question answering over the indexed bylaw corpus.

A thin HTTP shell over :class:`app.services.rag_service.RagService`. No
retrieval, prompting or scoring logic lives here — this module translates
between HTTP and the service contract and nothing else, which is what keeps the
whole pipeline testable without a web server.

**Abstention returns HTTP 200.** "I could not find anything in the indexed
bylaws that addresses this" is a correct, useful answer, not an error. Only
infrastructure failures — the model being unreachable, no index built — map to
5xx. Callers branch on ``outcome``, never on the status code alone.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, status

from app.api.deps import RagServiceDep
from app.core.logging import get_logger
from app.domain.provinces import find_municipality
from app.rag.retriever import RetrievalFilters
from app.schemas.ask import AskRequest, AskResponse, Citation, Confidence
from app.services.rag_service import AnswerOutcome, AnswerResult

__all__ = ["router"]

logger = get_logger(__name__)

router = APIRouter(tags=["ask"])


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask a question about a municipal sign bylaw",
    description=(
        "Answers strictly from indexed bylaw text. Every statement carries a "
        "citation with municipality, bylaw, section and page, and the quoted "
        "text is verified to appear verbatim in the cited chunk before the "
        "answer is returned.\n\n"
        "Check `outcome` before rendering. `answered` is the only outcome that "
        "is an answer; the rest explain why the system declined, and each needs "
        "different treatment in the interface. Abstentions are HTTP 200."
    ),
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Unknown municipality slug."},
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "No index built, or the language model is unreachable."
        },
    },
)
async def ask(payload: AskRequest, service: RagServiceDep) -> AskResponse:
    started = time.perf_counter()

    filters = None
    if payload.municipality:
        found = find_municipality(payload.municipality)
        if found is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"Unknown municipality '{payload.municipality}'. "
                    "Use a slug from GET /municipalities."
                ),
            )
        # in_force_only is not configurable by the caller. Answering from
        # repealed text is not a preference.
        filters = RetrievalFilters(
            municipality_slugs=(found[1].slug,), in_force_only=True
        )

    result = await service.answer(payload.question, filters=filters, top_n=payload.top_n)

    if result.outcome.is_infrastructure_failure:
        # Distinguished from an abstention: this is our fault, and a caller
        # should retry rather than tell the user the bylaw is silent.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result.answer
        )

    return _to_response(result, took_ms=int((time.perf_counter() - started) * 1000))


def _to_response(result: AnswerResult, *, took_ms: int) -> AskResponse:
    confidence = (
        Confidence(
            score=result.confidence.score,
            band=result.confidence.band.value,
            explanation=result.confidence.explanation,
            warnings=list(result.confidence.warnings),
        )
        if result.confidence is not None
        else None
    )

    return AskResponse(
        outcome=result.outcome.value,
        answered=result.outcome is AnswerOutcome.ANSWERED,
        answer=result.answer,
        citations=[
            Citation(
                municipality=citation.municipality,
                bylaw_title=citation.bylaw_title,
                bylaw_number=citation.bylaw_number,
                section=citation.section,
                section_path=citation.section_path,
                page=citation.page,
                quote=citation.quote,
                amendment_status=citation.amendment_status,
                document_id=citation.document_id,
                # Serving the source PDF is Phase 6. Left null rather than
                # pointing at a route that does not exist yet — a citation
                # link that 404s is worse than no link, because it reads as
                # evidence having been checked.
                source_url=None,
                from_ocr=citation.from_ocr,
            )
            for citation in result.citations
        ],
        confidence=confidence,
        clarification_options=list(result.clarification_options),
        outdated_documents=list(result.outdated_documents),
        conditions=list(result.conditions),
        trace_id=result.trace.trace_id if result.trace else None,
        took_ms=took_ms,
    )
