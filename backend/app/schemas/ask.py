"""Question and answer response contract.

Mirrors :class:`app.services.rag_service.AnswerResult` rather than inventing a
second shape. The pipeline's failure modes are part of the contract: a caller
must be able to tell "the bylaw is silent on this" from "the model is down"
from "which Langley did you mean", because those need three different responses
in the interface.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AskRequest", "AskResponse", "Citation", "Confidence"]


class AskRequest(BaseModel):
    """A question, optionally scoped to a municipality."""

    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=3, max_length=2000)
    municipality: str | None = Field(
        default=None,
        description=(
            "Municipality slug from /municipalities. When supplied, retrieval is "
            "restricted to it. When omitted, the municipality is inferred from "
            "the question text — and an ambiguous name returns "
            "needs_clarification rather than a guess."
        ),
    )
    # Deliberately not exposed: in_force_only. Answering from repealed text is
    # never a caller's choice to make.
    top_n: int | None = Field(
        default=None, ge=1, le=20, description="Chunks to consider. Defaults to config."
    )


class Citation(BaseModel):
    """Where a statement came from.

    Every field here exists so a reader can independently verify the claim
    against the source PDF. ``quote`` is checked to appear verbatim in the
    cited chunk before the answer is returned.
    """

    model_config = ConfigDict(frozen=True)

    municipality: str | None
    bylaw_title: str | None
    bylaw_number: str | None
    section: str | None
    section_path: str | None
    page: int
    quote: str
    amendment_status: str = Field(description="in_force, superseded, repealed or unknown.")
    document_id: str = ""
    source_url: str | None = Field(
        default=None, description="Link to the source PDF at the cited page."
    )
    from_ocr: bool = Field(
        default=False,
        description=(
            "True when the text was recovered by OCR rather than read from a "
            "text layer. Worth surfacing: OCR errors are plausible-looking."
        ),
    )


class Confidence(BaseModel):
    """How much the system trusts its own answer, and why."""

    model_config = ConfigDict(frozen=True)

    score: float = Field(ge=0.0, le=1.0)
    band: str = Field(description="high, medium, low or insufficient.")
    explanation: str
    warnings: list[str] = Field(default_factory=list)


class AskResponse(BaseModel):
    """The answer, its evidence, and how the pipeline resolved."""

    model_config = ConfigDict(frozen=True)

    outcome: str = Field(
        description=(
            "answered, needs_clarification, out_of_scope, no_relevant_bylaw, "
            "only_outdated, conflicting_amendments, unverified, "
            "generation_unavailable or index_not_ready."
        )
    )
    answered: bool = Field(
        description="False for every outcome other than 'answered'. Do not "
        "render an abstention as though it were an answer."
    )
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    confidence: Confidence | None = None

    # Populated when the municipality could not be resolved. The options are
    # official names — "City of Langley", "Township of Langley" — because the
    # bare name is precisely what was ambiguous.
    clarification_options: list[str] = Field(default_factory=list)
    # Documents found but excluded as superseded or repealed.
    outdated_documents: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)

    trace_id: str | None = Field(
        default=None,
        description="Correlates with the stored pipeline trace, so a disputed "
        "answer can be reconstructed.",
    )
    took_ms: int = 0
