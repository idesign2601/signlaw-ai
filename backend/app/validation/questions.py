"""Milestone 1 validation questions.

Distinct from :mod:`app.eval.dataset`. The golden set asserts *known correct
answers* and needs a human to verify each one against the real bylaw. This set
measures *system behaviour* against real documents without ground truth, so it
can run the day the corpus is ingested.

Everything here is measurable without knowing the right answer:

* did retrieval stay inside the municipality that was asked about
* did the model cite, and do the quotes actually appear in the retrieved text
* did it abstain, and on which kinds of question
* how long each stage took
* how confidence is distributed

What is *not* measurable without a human: whether the answer is factually
correct. The harness generates a spot-check worksheet for that rather than
guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["VALIDATION_QUESTIONS", "QuestionClass", "ValidationQuestion"]


class QuestionClass(StrEnum):
    """What behaviour a question probes."""

    FACT = "fact"
    """A specific limit. Should answer with a citation, or state its conditions."""

    PERMISSION = "permission"
    """Whether something is allowed. Silence must not be read as permission."""

    PROCEDURE = "procedure"
    """Permit and application requirements."""

    COMPARISON = "comparison"
    """Two or more municipalities. Must cover each for every aspect."""

    DEFINITION = "definition"
    """What a term means in the bylaw."""

    AMBIGUITY = "ambiguity"
    """Must ask which municipality rather than picking one."""

    OUT_OF_SCOPE = "out_of_scope"
    """Must decline without retrieving."""

    UNANSWERABLE = "unanswerable"
    """In scope but absent from any bylaw. Must abstain, not reason by analogy."""


@dataclass(frozen=True, slots=True)
class ValidationQuestion:
    """One probe."""

    id: str
    question: str
    kind: QuestionClass
    # Municipalities retrieval should stay within. Empty means unconstrained.
    expected_municipalities: tuple[str, ...] = ()
    # Whether declining is the correct outcome.
    expect_abstention: bool = False
    expect_clarification: bool = False
    # Whether the correct answer names conditions (zone, sign type) rather than
    # a single number.
    expect_conditional: bool = False
    notes: str = ""

    @property
    def expects_answer(self) -> bool:
        return not (self.expect_abstention or self.expect_clarification)


_CITIES = ("burnaby", "vancouver", "surrey")


def _per_city(
    suffix: str,
    template: str,
    kind: QuestionClass,
    *,
    conditional: bool = False,
    notes: str = "",
) -> tuple[ValidationQuestion, ...]:
    """Build the same question for each validated municipality.

    Asking all three the same thing is what makes cross-city comparison of the
    metrics meaningful — a retrieval precision gap between cities points at a
    document-quality problem rather than a pipeline problem.
    """
    return tuple(
        ValidationQuestion(
            id=f"{city}-{suffix}",
            question=template.format(city=city.title()),
            kind=kind,
            expected_municipalities=(city,),
            expect_conditional=conditional,
            notes=notes,
        )
        for city in _CITIES
    )


VALIDATION_QUESTIONS: tuple[ValidationQuestion, ...] = (
    # --- the questions from the milestone brief, per city ------------------
    *_per_city(
        "max-fascia-area",
        "What is the maximum fascia sign area in {city}?",
        QuestionClass.FACT,
        conditional=True,
        notes=(
            "Almost certainly depends on zone and building frontage. A single "
            "unconditional number is a red flag even if it looks plausible."
        ),
    ),
    *_per_city(
        "projecting-permitted",
        "Are projecting signs permitted in {city}?",
        QuestionClass.PERMISSION,
        notes="Watch for silence being reported as permission.",
    ),
    *_per_city(
        "illuminated-permit",
        "What permit is required for an illuminated sign in {city}?",
        QuestionClass.PROCEDURE,
        notes="Procedural text often sits in a different part from the limits.",
    ),
    # --- comparison --------------------------------------------------------
    ValidationQuestion(
        id="cmp-burnaby-vancouver",
        question="Compare Burnaby and Vancouver sign requirements.",
        kind=QuestionClass.COMPARISON,
        expected_municipalities=("burnaby", "vancouver"),
        notes=(
            "From the brief. Must cover both cities for each aspect. Check the "
            "retrieval split is roughly balanced — a lopsided pull means the "
            "fan-out is not working."
        ),
    ),
    ValidationQuestion(
        id="cmp-three-city-temporary",
        question=("Compare temporary sign regulations between Burnaby, Vancouver and Surrey."),
        kind=QuestionClass.COMPARISON,
        expected_municipalities=_CITIES,
        notes="Three-way fan-out. Stresses context assembly against the window.",
    ),
    ValidationQuestion(
        id="cmp-projecting-two-city",
        question="How do Surrey and Vancouver differ on projecting signs?",
        kind=QuestionClass.COMPARISON,
        expected_municipalities=("surrey", "vancouver"),
    ),
    # --- more coverage of real regulatory surface --------------------------
    *_per_city(
        "sandwich-board",
        "Are sandwich board signs allowed on the sidewalk in {city}?",
        QuestionClass.PERMISSION,
    ),
    *_per_city(
        "window-signs",
        "What are the rules for window signs in {city}?",
        QuestionClass.FACT,
        conditional=True,
    ),
    *_per_city(
        "freestanding-height",
        "What is the maximum height for a freestanding sign in {city}?",
        QuestionClass.FACT,
        conditional=True,
        notes="Height limits are usually tabular. Tests table survival.",
    ),
    ValidationQuestion(
        id="def-fascia-sign",
        question="What is the definition of a fascia sign?",
        kind=QuestionClass.DEFINITION,
        notes="Should retrieve a definition chunk, not a regulatory clause.",
    ),
    ValidationQuestion(
        id="def-sign-area",
        question="How is sign area measured?",
        kind=QuestionClass.DEFINITION,
        notes=(
            "Measurement method is definitional and materially changes every "
            "numeric answer. Worth confirming it retrieves cleanly."
        ),
    ),
    # --- behavioural probes -------------------------------------------------
    ValidationQuestion(
        id="amb-bare-langley",
        question="What are the sign rules in Langley?",
        kind=QuestionClass.AMBIGUITY,
        expect_clarification=True,
        notes=(
            "City and Township of Langley have separate bylaws. Neither is in "
            "the validation corpus, so this tests routing, not retrieval."
        ),
    ),
    ValidationQuestion(
        id="amb-bare-north-vancouver",
        question="Can I install a banner in North Vancouver?",
        kind=QuestionClass.AMBIGUITY,
        expect_clarification=True,
    ),
    ValidationQuestion(
        id="oos-population",
        question="What is the population of Surrey?",
        kind=QuestionClass.OUT_OF_SCOPE,
        expect_abstention=True,
        notes=(
            "The dangerous shape: a factual question about a municipality in "
            "the corpus that the model could answer from memory."
        ),
    ),
    ValidationQuestion(
        id="oos-weather",
        question="What is the weather in Vancouver today?",
        kind=QuestionClass.OUT_OF_SCOPE,
        expect_abstention=True,
    ),
    ValidationQuestion(
        id="oos-business-licence",
        question="How much does a business licence cost in Burnaby?",
        kind=QuestionClass.OUT_OF_SCOPE,
        expect_abstention=True,
        notes=(
            "Municipal, plausible, and governed by a different bylaw entirely. "
            "The most likely false-answer case in this set."
        ),
    ),
    ValidationQuestion(
        id="unans-holographic",
        question="What are the rules for holographic projection signs in Surrey?",
        kind=QuestionClass.UNANSWERABLE,
        expected_municipalities=("surrey",),
        expect_abstention=True,
        notes="Must not reason by analogy from illuminated or digital signs.",
    ),
    ValidationQuestion(
        id="unans-drone",
        question="Are drone-mounted advertising signs permitted in Vancouver?",
        kind=QuestionClass.UNANSWERABLE,
        expected_municipalities=("vancouver",),
        expect_abstention=True,
    ),
    ValidationQuestion(
        id="unans-uncovered-city",
        question="What is the maximum fascia sign area in Kelowna?",
        kind=QuestionClass.UNANSWERABLE,
        expected_municipalities=("kelowna",),
        expect_abstention=True,
        notes=(
            "Kelowna is deliberately outside the validation corpus. Must abstain "
            "rather than answering from Burnaby, Vancouver or Surrey. This is "
            "the single most important probe in the set."
        ),
    ),
)


def questions_for(city: str) -> tuple[ValidationQuestion, ...]:
    return tuple(
        question for question in VALIDATION_QUESTIONS if city in question.expected_municipalities
    )


def behavioural_questions() -> tuple[ValidationQuestion, ...]:
    """Questions where declining is the correct outcome."""
    return tuple(question for question in VALIDATION_QUESTIONS if not question.expects_answer)
