"""Citation enforcement.

Prompting a model to stay grounded reduces fabrication. It does not eliminate
it, and the residual rate is not acceptable when the output is a legal citation
someone will build to. So every answer is checked against the evidence it claims
to rest on, and anything unsupported is removed before the user sees it.

Four checks, cheapest first:

1. **Resolution** — every ``[S#]`` marker refers to an excerpt that was actually
   retrieved. A marker pointing at nothing is a fabricated source.
2. **Quote verification** — the verbatim quote appears in that excerpt's text.
   This is the strongest available signal, because a model that invents a
   requirement almost never invents a quote that happens to be present.
3. **Numeric grounding** — every number in the answer appears in some cited
   excerpt. Numbers are what people act on, and a plausible-but-wrong dimension
   is the most damaging error this system can make.
4. **Claim coverage** — statements that assert an obligation, permission or
   prohibition carry a citation.

Failures do not raise. They strip the offending claim and lower confidence, and
when too little survives the answer is replaced by an abstention — because "I
don't know" is a correct answer and a fabricated citation never is.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.rag.results import RetrievedChunk

__all__ = ["CitationClaim", "CitationVerifier", "VerificationReport"]

logger = get_logger(__name__)

_WHITESPACE = re.compile(r"\s+")

# Numbers that carry regulatory meaning. Bare years and bylaw numbers are
# excluded by _is_regulatory_number below.
_NUMBER = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|m|mm|cm|metres?|meters?|"
    r"ft|feet|foot|in|inches|sq\.?\s*m|square\s+met(?:re|er)s?)?"
)

# Sentences asserting a rule. These are what must be cited; descriptive or
# hedging sentences need not be.
_OBLIGATION = re.compile(
    r"\b(must|shall|may not|cannot|can not|is not permitted|are not permitted|"
    r"is permitted|are permitted|is allowed|are allowed|is prohibited|"
    r"are prohibited|requires?|required|maximum|minimum|no more than|"
    r"no less than|up to|limited to|exceed)\b",
    re.IGNORECASE,
)

# Hedges that mark a sentence as meta-commentary rather than a legal claim.
_NON_CLAIM = re.compile(
    r"^\s*(?:the (?:excerpts?|bylaws?|documents?|provided)|"
    r"i (?:cannot|could not|don't|do not)|"
    r"this (?:depends|varies)|"
    r"note that|however|based on|according to)\b",
    re.IGNORECASE,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")

_MARKER = re.compile(r"\[S(\d+)\]")


def _normalise(text: str) -> str:
    """Collapse whitespace for comparison.

    PDF extraction introduces line breaks and runs of spaces that are
    meaningless to a quote match but would defeat a literal comparison.
    """
    return _WHITESPACE.sub(" ", text).strip().casefold()


@dataclass(frozen=True, slots=True)
class CitationClaim:
    """One citation the model produced, and whether it holds up."""

    source_id: int
    quote: str
    supports: str
    chunk_id: str | None = None
    resolved: bool = False
    quote_verified: bool = False

    @property
    def is_valid(self) -> bool:
        return self.resolved and self.quote_verified

    @property
    def failure_reason(self) -> str | None:
        if not self.resolved:
            return f"[S{self.source_id}] does not match any retrieved excerpt"
        if not self.quote_verified:
            return "quoted text does not appear in the cited excerpt"
        return None


@dataclass
class VerificationReport:
    """What survived verification, and what did not."""

    valid_claims: list[CitationClaim] = field(default_factory=list)
    invalid_claims: list[CitationClaim] = field(default_factory=list)
    # Sentences asserting a rule with no citation attached.
    uncited_claims: list[str] = field(default_factory=list)
    # Numbers in the answer absent from every cited excerpt.
    ungrounded_numbers: list[str] = field(default_factory=list)
    should_abstain: bool = False
    abstain_reason: str | None = None

    @property
    def citation_precision(self) -> float:
        """Fraction of the model's citations that check out."""
        total = len(self.valid_claims) + len(self.invalid_claims)
        return len(self.valid_claims) / total if total else 0.0

    @property
    def is_clean(self) -> bool:
        return not self.invalid_claims and not self.uncited_claims and not self.ungrounded_numbers

    @property
    def cited_chunk_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(claim.chunk_id for claim in self.valid_claims if claim.chunk_id))

    def as_dict(self) -> dict[str, object]:
        return {
            "valid_citations": len(self.valid_claims),
            "invalid_citations": len(self.invalid_claims),
            "uncited_claims": self.uncited_claims,
            "ungrounded_numbers": self.ungrounded_numbers,
            "citation_precision": round(self.citation_precision, 3),
            "should_abstain": self.should_abstain,
            "abstain_reason": self.abstain_reason,
        }


@dataclass
class CitationVerifier:
    """Checks an answer against the excerpts it claims to rest on.

    Parameters:
        min_citation_precision: Below this fraction of valid citations, the
            answer is discarded entirely. A model citing badly once may be
            unlucky; citing badly half the time is not producing evidence.
        require_citations: Whether an answer asserting rules with no valid
            citation at all should abstain.
    """

    min_citation_precision: float = 0.6
    require_citations: bool = True
    # Quote matching tolerates minor whitespace and case differences but not
    # paraphrase: a "quote" the model reworded is not evidence.
    allow_partial_quotes: bool = True

    def verify(
        self,
        answer: str,
        claims: Sequence[CitationClaim],
        chunks: Sequence[RetrievedChunk],
        *,
        source_map: dict[int, str],
    ) -> VerificationReport:
        """Verify an answer. Never raises."""
        report = VerificationReport()
        by_id = {chunk.chunk_id: chunk for chunk in chunks}

        for claim in claims:
            checked = self._check_claim(claim, source_map, by_id)
            if checked.is_valid:
                report.valid_claims.append(checked)
            else:
                report.invalid_claims.append(checked)
                logger.warning(
                    "citation_rejected",
                    source_id=claim.source_id,
                    reason=checked.failure_reason,
                )

        cited_text = " ".join(
            by_id[claim.chunk_id].body
            for claim in report.valid_claims
            if claim.chunk_id and claim.chunk_id in by_id
        )

        report.uncited_claims = self._uncited_claims(answer)
        report.ungrounded_numbers = self._ungrounded_numbers(answer, cited_text)

        self._decide_abstention(report, answer)
        return report

    # -- individual checks ---------------------------------------------------

    def _check_claim(
        self,
        claim: CitationClaim,
        source_map: dict[int, str],
        by_id: dict[str, RetrievedChunk],
    ) -> CitationClaim:
        chunk_id = source_map.get(claim.source_id)
        if chunk_id is None or chunk_id not in by_id:
            return CitationClaim(
                source_id=claim.source_id,
                quote=claim.quote,
                supports=claim.supports,
                chunk_id=chunk_id,
                resolved=False,
            )

        verified = self._quote_appears_in(claim.quote, by_id[chunk_id].body)
        return CitationClaim(
            source_id=claim.source_id,
            quote=claim.quote,
            supports=claim.supports,
            chunk_id=chunk_id,
            resolved=True,
            quote_verified=verified,
        )

    def _quote_appears_in(self, quote: str, body: str) -> bool:
        """Whether a quote genuinely appears in the excerpt."""
        needle = _normalise(quote)
        if not needle:
            return False

        haystack = _normalise(body)
        if needle in haystack:
            return True

        if not self.allow_partial_quotes:
            return False

        # A quote spanning a hyphenated line break or a stripped table pipe
        # will not match literally. Requiring most of a long quote's word
        # sequence to be present tolerates that without tolerating paraphrase.
        words = needle.split()
        if len(words) < 8:
            return False

        window = " ".join(words[: max(6, len(words) * 2 // 3)])
        return window in haystack

    def _uncited_claims(self, answer: str) -> list[str]:
        """Sentences asserting a rule with no ``[S#]`` marker."""
        if not self.require_citations:
            return []

        uncited: list[str] = []
        for sentence in _SENTENCE_SPLIT.split(answer):
            stripped = sentence.strip()
            if not stripped or _NON_CLAIM.match(stripped):
                continue
            if not _OBLIGATION.search(stripped):
                continue
            if _MARKER.search(stripped):
                continue
            uncited.append(stripped)
        return uncited

    @staticmethod
    def _ungrounded_numbers(answer: str, cited_text: str) -> list[str]:
        """Numbers in the answer absent from every cited excerpt.

        The check people care about most. A fabricated 6-metre height limit
        reads exactly like a real one, and someone will build to it.
        """
        if not cited_text:
            return []

        haystack = _normalise(cited_text)
        ungrounded: list[str] = []

        for match in _NUMBER.finditer(answer):
            token = match.group(0).strip()
            digits = re.sub(r"[^\d.]", "", token)
            if not _is_regulatory_number(digits):
                continue
            # Match on the bare number: the excerpt may express the same value
            # with different unit spacing or abbreviation.
            if digits and digits not in haystack.replace(",", ""):
                ungrounded.append(token)

        return list(dict.fromkeys(ungrounded))

    # -- abstention ----------------------------------------------------------

    def _decide_abstention(self, report: VerificationReport, answer: str) -> None:
        """Decide whether too little survived to show the answer at all."""
        asserts_rules = bool(_OBLIGATION.search(answer))

        if self.require_citations and asserts_rules and not report.valid_claims:
            report.should_abstain = True
            report.abstain_reason = (
                "the answer asserts bylaw requirements but no citation could be "
                "verified against the retrieved excerpts"
            )
            return

        total = len(report.valid_claims) + len(report.invalid_claims)
        if total and report.citation_precision < self.min_citation_precision:
            report.should_abstain = True
            report.abstain_reason = (
                f"only {report.citation_precision:.0%} of citations could be "
                f"verified, below the {self.min_citation_precision:.0%} threshold"
            )
            return

        if report.ungrounded_numbers:
            report.should_abstain = True
            report.abstain_reason = (
                f"the answer states values not present in any cited excerpt: "
                f"{', '.join(report.ungrounded_numbers[:3])}"
            )


def _is_regulatory_number(digits: str) -> bool:
    """Whether a number is a limit rather than a year or a bylaw number.

    Years and bylaw numbers appear constantly in these answers and are not
    dimensional claims, so checking them would produce noise that drowns the
    signal.
    """
    if not digits:
        return False
    try:
        value = float(digits)
    except ValueError:
        return False

    # Four-digit integers in the plausible year range, and larger integers, are
    # almost always years or bylaw numbers rather than dimensions.
    if value.is_integer() and 1800 <= value <= 2200:
        return False
    return not (value.is_integer() and value > 9999)
