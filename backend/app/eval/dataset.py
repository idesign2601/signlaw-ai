"""Golden evaluation set.

The regression suite for answer quality. Without it, changing a chunk size or
swapping an embedding model is a blind edit: the system still returns confident,
well-formatted answers, and there is no way to tell whether they got better or
worse. For a product whose output is a legal citation, that is not an acceptable
place to be.

Each case states the expected municipality, section and page, so three
independent things can be measured:

* **Retrieval accuracy** — did the right section reach the model at all? A
  citation failure downstream is meaningless if retrieval never surfaced the
  clause.
* **Citation accuracy** — did the answer cite the right section, and does the
  quote actually appear there?
* **Outdated-document prevention** — did any citation rest on superseded or
  repealed text? This is scored separately and treated as a hard failure, not a
  points deduction.

Cases are declarative and corpus-independent: they name a municipality and a
subject, not a document ID, so the set survives re-ingestion. ``expected_*``
fields are filled in by whoever verifies the case against the real bylaw.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

__all__ = ["EvalCase", "EvalKind", "EvalSuite", "SEED_CASES"]


class EvalKind(StrEnum):
    """What a case is testing."""

    FACT_LOOKUP = "fact_lookup"
    """A specific numeric or categorical limit."""

    PERMISSION = "permission"
    """Whether something is allowed."""

    DEFINITION = "definition"
    """What a term means in the bylaw."""

    COMPARISON = "comparison"
    """Regulations across two or more municipalities."""

    AMBIGUITY = "ambiguity"
    """Should ask for clarification rather than answer."""

    OUT_OF_SCOPE = "out_of_scope"
    """Should decline: not a sign-bylaw question."""

    ABSTENTION = "abstention"
    """In scope but unanswerable from the corpus; must not fabricate."""


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One question with its verified expectations."""

    id: str
    question: str
    kind: EvalKind

    # Municipality slugs the answer must be scoped to.
    expected_municipalities: tuple[str, ...] = ()
    # Section numbers that would constitute a correct citation. Several are
    # allowed because a question is often answered by any of a few clauses.
    acceptable_sections: tuple[str, ...] = ()
    expected_pages: tuple[int, ...] = ()
    # Substrings that must appear in a correct answer, e.g. "20%".
    expected_answer_contains: tuple[str, ...] = ()
    # Substrings that must NOT appear — usually a known-wrong number that a
    # neighbouring clause or a superseded version would produce.
    must_not_contain: tuple[str, ...] = ()

    # True when the correct behaviour is to decline.
    should_abstain: bool = False
    should_ask_clarification: bool = False
    # True when the answer must state that it depends on zone or sign type.
    answer_is_conditional: bool = False

    notes: str = ""
    verified_by: str | None = None
    verified_on: str | None = None

    @property
    def is_verified(self) -> bool:
        """Whether a human has confirmed this case against the real bylaw.

        Unverified cases still exercise the pipeline but must not be counted in
        an accuracy figure — a golden set full of guesses measures nothing.
        """
        return bool(self.verified_by)


@dataclass
class EvalSuite:
    """A collection of cases, loadable from and savable to JSON."""

    cases: list[EvalCase] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path) -> EvalSuite:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(cases=[_case_from_dict(entry) for entry in raw.get("cases", [])])

    def to_file(self, path: Path) -> None:
        payload = {"cases": [asdict(case) for case in self.cases]}
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def verified_only(self) -> EvalSuite:
        return EvalSuite(cases=[case for case in self.cases if case.is_verified])

    def by_kind(self, kind: EvalKind) -> EvalSuite:
        return EvalSuite(cases=[case for case in self.cases if case.kind is kind])

    def by_municipality(self, slug: str) -> EvalSuite:
        return EvalSuite(
            cases=[
                case for case in self.cases if slug in case.expected_municipalities
            ]
        )

    def __len__(self) -> int:
        return len(self.cases)


def _case_from_dict(entry: dict[str, object]) -> EvalCase:
    return EvalCase(
        id=str(entry["id"]),
        question=str(entry["question"]),
        kind=EvalKind(str(entry.get("kind", EvalKind.FACT_LOOKUP))),
        expected_municipalities=tuple(entry.get("expected_municipalities") or ()),  # type: ignore[arg-type]
        acceptable_sections=tuple(entry.get("acceptable_sections") or ()),  # type: ignore[arg-type]
        expected_pages=tuple(entry.get("expected_pages") or ()),  # type: ignore[arg-type]
        expected_answer_contains=tuple(entry.get("expected_answer_contains") or ()),  # type: ignore[arg-type]
        must_not_contain=tuple(entry.get("must_not_contain") or ()),  # type: ignore[arg-type]
        should_abstain=bool(entry.get("should_abstain", False)),
        should_ask_clarification=bool(entry.get("should_ask_clarification", False)),
        answer_is_conditional=bool(entry.get("answer_is_conditional", False)),
        notes=str(entry.get("notes", "")),
        verified_by=(str(entry["verified_by"]) if entry.get("verified_by") else None),
        verified_on=(str(entry["verified_on"]) if entry.get("verified_on") else None),
    )


# -----------------------------------------------------------------------------
# Seed cases
#
# These exercise every behaviour the pipeline must get right. Section numbers
# and expected values are LEFT BLANK deliberately: filling them in with guesses
# would produce a suite that passes while measuring nothing. Each case needs one
# person to open the real bylaw, record the section and page, and set
# `verified_by`. Cases covering behaviour rather than content — ambiguity,
# out-of-scope, abstention — are verified already, because their correct
# behaviour does not depend on the corpus.
# -----------------------------------------------------------------------------

SEED_CASES: tuple[EvalCase, ...] = (
    # --- fact lookup -------------------------------------------------------
    EvalCase(
        id="van-fascia-height",
        question="What is the maximum fascia sign height in Vancouver?",
        kind=EvalKind.FACT_LOOKUP,
        expected_municipalities=("vancouver",),
        answer_is_conditional=True,
        notes=(
            "Almost certainly depends on zoning district. A single unconditional "
            "number is a wrong answer even if that number appears somewhere in "
            "the bylaw. Fill in acceptable_sections from the real bylaw."
        ),
    ),
    EvalCase(
        id="van-max-sign-area",
        question="What is the maximum sign area in Vancouver?",
        kind=EvalKind.FACT_LOOKUP,
        expected_municipalities=("vancouver",),
        answer_is_conditional=True,
        notes=(
            "Answered by a table (zone x sign type), not prose. Tests that table "
            "extraction survived chunking and that the answer presents conditions "
            "rather than picking one cell."
        ),
    ),
    EvalCase(
        id="burnaby-projecting",
        question="Are projecting signs allowed in Burnaby?",
        kind=EvalKind.PERMISSION,
        expected_municipalities=("burnaby",),
        notes="Tests permission phrasing and that silence is not read as permission.",
    ),
    EvalCase(
        id="burnaby-window-graphics-permit",
        question="Does Burnaby require permits for window graphics?",
        kind=EvalKind.PERMISSION,
        expected_municipalities=("burnaby",),
        notes="From the project brief.",
    ),
    EvalCase(
        id="coquitlam-fascia-permitted",
        question="Can I install a fascia sign in Coquitlam?",
        kind=EvalKind.PERMISSION,
        expected_municipalities=("coquitlam",),
        notes="From the project brief.",
    ),
    # --- definition --------------------------------------------------------
    EvalCase(
        id="def-fascia-sign",
        question="What counts as a fascia sign?",
        kind=EvalKind.DEFINITION,
        notes=(
            "Should retrieve a definition chunk, not a regulatory clause. Tests "
            "that per-term definition chunking works."
        ),
    ),
    # --- comparison --------------------------------------------------------
    EvalCase(
        id="cmp-surrey-richmond-temporary",
        question="Compare Surrey and Richmond temporary sign regulations.",
        kind=EvalKind.COMPARISON,
        expected_municipalities=("surrey", "richmond"),
        notes=(
            "Must cover both cities for each aspect, and say so explicitly where "
            "one city's bylaw is silent. A missing city reads as 'no restriction'."
        ),
    ),
    EvalCase(
        id="cmp-langley-city-vs-township",
        question=(
            "Compare sign regulations between the City of Langley and the "
            "Township of Langley."
        ),
        kind=EvalKind.COMPARISON,
        expected_municipalities=("langley-city", "langley-township"),
        notes=(
            "The hardest comparison in the corpus: two municipalities sharing a "
            "name with separate bylaws. Tests that qualified names resolve to "
            "distinct jurisdictions and citations do not cross between them."
        ),
    ),
    # --- ambiguity ---------------------------------------------------------
    EvalCase(
        id="amb-bare-langley",
        question="What are the sign rules in Langley?",
        kind=EvalKind.AMBIGUITY,
        should_ask_clarification=True,
        notes=(
            "City and Township of Langley have separate bylaws. Answering from "
            "either without asking is a plausible-looking wrong answer."
        ),
        verified_by="design",
        verified_on="2026-08-04",
    ),
    EvalCase(
        id="amb-bare-north-vancouver",
        question="Can I put up a banner in North Vancouver?",
        kind=EvalKind.AMBIGUITY,
        should_ask_clarification=True,
        notes="City and District of North Vancouver are distinct jurisdictions.",
        verified_by="design",
        verified_on="2026-08-04",
    ),
    # --- out of scope ------------------------------------------------------
    EvalCase(
        id="oos-weather",
        question="What is the weather in Vancouver today?",
        kind=EvalKind.OUT_OF_SCOPE,
        should_abstain=True,
        notes="Names a BC municipality but is not a sign-bylaw question.",
        verified_by="design",
        verified_on="2026-08-04",
    ),
    EvalCase(
        id="oos-population",
        question="What is the population of Surrey?",
        kind=EvalKind.OUT_OF_SCOPE,
        should_abstain=True,
        notes=(
            "The dangerous shape: a factual question about a municipality that "
            "the model could answer from memory and must not."
        ),
        verified_by="design",
        verified_on="2026-08-04",
    ),
    # --- abstention --------------------------------------------------------
    EvalCase(
        id="abs-uncovered-municipality",
        question="What are the fascia sign rules in Whistler?",
        kind=EvalKind.ABSTENTION,
        expected_municipalities=("whistler",),
        should_abstain=True,
        notes=(
            "Set up by omitting Whistler from the test corpus. Must abstain "
            "rather than answering from a neighbouring municipality's bylaw."
        ),
    ),
    EvalCase(
        id="abs-invented-sign-type",
        question="What are the rules for holographic projection signs in Surrey?",
        kind=EvalKind.ABSTENTION,
        expected_municipalities=("surrey",),
        should_abstain=True,
        notes=(
            "A sign type no BC bylaw addresses. Must say the bylaw does not "
            "cover it, not reason by analogy from illuminated signs."
        ),
        verified_by="design",
        verified_on="2026-08-04",
    ),
)


def seed_suite() -> EvalSuite:
    """The bundled starting suite."""
    return EvalSuite(cases=list(SEED_CASES))
