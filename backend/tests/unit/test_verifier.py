"""Citation enforcement.

The tests that matter most in the project. Each one encodes a way a fluent model
produces an answer that reads perfectly and is wrong — a cited source that was
never retrieved, a quote that was paraphrased, a dimension that appears nowhere
in the evidence.
"""

from __future__ import annotations

import pytest

from app.db.enums import ChunkType, DocumentStatus
from app.rag.results import RetrievedChunk
from app.rag.verifier import CitationClaim, CitationVerifier

BODY = (
    "5.3 Fascia Signs\n"
    "(a) A fascia sign must not exceed twenty percent (20%) of the area of the "
    "building face to which it is attached.\n"
    "(b) No fascia sign shall project more than 0.3 metres from the building face."
)


def chunk(chunk_id: str = "c1", body: str = BODY, **overrides: object) -> RetrievedChunk:
    defaults: dict[str, object] = {
        "chunk_id": chunk_id,
        "body": body,
        "chunk_type": ChunkType.PROSE,
        "document_id": "doc-1",
        "document_title": "Sign Bylaw No. 4451",
        "municipality_slug": "coquitlam",
        "municipality_name": "Coquitlam",
        "bylaw_number": "4451",
        "section_number": "5.3",
        "section_path": "Part 5 > 5.3",
        "section_heading": "Fascia Signs",
        "page_number": 22,
        "document_status": DocumentStatus.IN_FORCE,
    }
    defaults.update(overrides)
    return RetrievedChunk(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def verifier() -> CitationVerifier:
    return CitationVerifier()


class TestResolution:
    def test_valid_citation_passes(self, verifier: CitationVerifier) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent", "20% limit")
        report = verifier.verify(
            "A fascia sign must not exceed 20% of the building face [S1].",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert report.valid_claims
        assert report.citation_precision == 1.0

    def test_citation_to_a_source_that_was_never_retrieved(
        self, verifier: CitationVerifier
    ) -> None:
        # The clearest fabrication signal: a marker pointing at nothing.
        claim = CitationClaim(7, "some text", "a claim")
        report = verifier.verify(
            "Signs must be under 3 metres [S7].", [claim], [chunk()], source_map={1: "c1"}
        )
        assert not report.valid_claims
        assert "does not match any retrieved excerpt" in (
            report.invalid_claims[0].failure_reason or ""
        )

    def test_source_map_pointing_at_a_missing_chunk(
        self, verifier: CitationVerifier
    ) -> None:
        claim = CitationClaim(1, "must not exceed", "x")
        report = verifier.verify(
            "text [S1]", [claim], [chunk("other")], source_map={1: "c1"}
        )
        assert not report.valid_claims


class TestQuoteVerification:
    def test_exact_quote_verifies(self, verifier: CitationVerifier) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify("x [S1]", [claim], [chunk()], source_map={1: "c1"})
        assert report.valid_claims

    def test_whitespace_differences_are_tolerated(
        self, verifier: CitationVerifier
    ) -> None:
        # PDF extraction inserts line breaks that mean nothing to the match.
        claim = CitationClaim(1, "must   not\n exceed twenty percent", "x")
        report = verifier.verify("x [S1]", [claim], [chunk()], source_map={1: "c1"})
        assert report.valid_claims

    def test_case_differences_are_tolerated(self, verifier: CitationVerifier) -> None:
        claim = CitationClaim(1, "MUST NOT EXCEED TWENTY PERCENT", "x")
        report = verifier.verify("x [S1]", [claim], [chunk()], source_map={1: "c1"})
        assert report.valid_claims

    def test_paraphrase_is_rejected(self, verifier: CitationVerifier) -> None:
        # A reworded "quote" is not evidence, however accurate the paraphrase.
        claim = CitationClaim(
            1, "fascia signs are capped at one fifth of the wall area", "x"
        )
        report = verifier.verify("x [S1]", [claim], [chunk()], source_map={1: "c1"})
        assert not report.valid_claims
        assert "does not appear" in (report.invalid_claims[0].failure_reason or "")

    def test_empty_quote_is_rejected(self, verifier: CitationVerifier) -> None:
        report = verifier.verify(
            "x [S1]", [CitationClaim(1, "", "x")], [chunk()], source_map={1: "c1"}
        )
        assert not report.valid_claims

    def test_partial_matching_can_be_disabled(self) -> None:
        strict = CitationVerifier(allow_partial_quotes=False)
        claim = CitationClaim(
            1,
            "A fascia sign must not exceed twenty percent (20%) of the area of "
            "the building face which it attaches to",
            "x",
        )
        report = strict.verify("x [S1]", [claim], [chunk()], source_map={1: "c1"})
        assert not report.valid_claims


class TestNumericGrounding:
    def test_number_present_in_the_source_passes(
        self, verifier: CitationVerifier
    ) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "A fascia sign must not exceed 20% of the building face [S1].",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert not report.ungrounded_numbers
        assert not report.should_abstain

    def test_fabricated_dimension_forces_abstention(
        self, verifier: CitationVerifier
    ) -> None:
        # The most damaging error available: a plausible number nobody wrote.
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "A fascia sign must not exceed 6.5 metres in height [S1].",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert "6.5" in " ".join(report.ungrounded_numbers)
        assert report.should_abstain

    def test_years_are_not_treated_as_dimensions(
        self, verifier: CitationVerifier
    ) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "Under the 2019 bylaw, a fascia sign must not exceed 20% [S1].",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert not report.ungrounded_numbers

    def test_bylaw_numbers_are_not_treated_as_dimensions(
        self, verifier: CitationVerifier
    ) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "Bylaw No. 13743 requires that signs must not exceed 20% [S1].",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert not report.ungrounded_numbers

    def test_grounding_is_skipped_without_citations(
        self, verifier: CitationVerifier
    ) -> None:
        report = verifier.verify("Signs may be 3 metres tall.", [], [chunk()], source_map={})
        assert report.ungrounded_numbers == []


class TestUncitedClaims:
    def test_uncited_obligation_is_flagged(self, verifier: CitationVerifier) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "A fascia sign must not exceed 20% [S1]. Projecting signs are prohibited.",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert any("prohibited" in text for text in report.uncited_claims)

    def test_hedged_sentences_are_not_claims(self, verifier: CitationVerifier) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "A fascia sign must not exceed 20% [S1]. "
            "The excerpts do not address illuminated signs.",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert not report.uncited_claims

    def test_descriptive_sentences_need_no_citation(
        self, verifier: CitationVerifier
    ) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "A fascia sign must not exceed 20% [S1]. This applies in Coquitlam.",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert not report.uncited_claims


class TestAbstention:
    def test_rules_asserted_with_no_valid_citation(
        self, verifier: CitationVerifier
    ) -> None:
        report = verifier.verify(
            "Fascia signs must not exceed 20% of the building face.",
            [],
            [chunk()],
            source_map={1: "c1"},
        )
        assert report.should_abstain
        assert "no citation could be verified" in (report.abstain_reason or "")

    def test_precision_below_threshold_abstains(self) -> None:
        verifier = CitationVerifier(min_citation_precision=0.6)
        claims = [
            CitationClaim(1, "must not exceed twenty percent (20%)", "ok"),
            CitationClaim(9, "invented", "bad"),
            CitationClaim(8, "also invented", "bad"),
        ]
        report = verifier.verify(
            "Signs must comply [S1][S9][S8].", claims, [chunk()], source_map={1: "c1"}
        )
        assert report.should_abstain
        assert "below the" in (report.abstain_reason or "")

    def test_clean_answer_does_not_abstain(self, verifier: CitationVerifier) -> None:
        claim = CitationClaim(1, "must not exceed twenty percent (20%)", "x")
        report = verifier.verify(
            "A fascia sign must not exceed 20% of the building face [S1].",
            [claim],
            [chunk()],
            source_map={1: "c1"},
        )
        assert not report.should_abstain
        assert report.is_clean

    def test_descriptive_answer_without_citations_does_not_abstain(
        self, verifier: CitationVerifier
    ) -> None:
        # No rule is asserted, so there is nothing to fabricate.
        report = verifier.verify(
            "The excerpts describe several categories of signage.",
            [],
            [chunk()],
            source_map={1: "c1"},
        )
        assert not report.should_abstain

    def test_enforcement_can_be_relaxed(self) -> None:
        lenient = CitationVerifier(require_citations=False)
        report = lenient.verify(
            "Fascia signs must not exceed 20%.", [], [chunk()], source_map={1: "c1"}
        )
        assert not report.should_abstain


class TestReporting:
    def test_cited_chunk_ids_are_deduplicated(self, verifier: CitationVerifier) -> None:
        claims = [
            CitationClaim(1, "must not exceed twenty percent", "a"),
            CitationClaim(1, "shall project more than 0.3 metres", "b"),
        ]
        report = verifier.verify(
            "text [S1]", claims, [chunk()], source_map={1: "c1"}
        )
        assert report.cited_chunk_ids == ("c1",)

    def test_report_serialises(self, verifier: CitationVerifier) -> None:
        report = verifier.verify("text", [], [chunk()], source_map={})
        payload = report.as_dict()
        assert "citation_precision" in payload
        assert "should_abstain" in payload

    def test_precision_with_no_citations_is_zero(
        self, verifier: CitationVerifier
    ) -> None:
        assert verifier.verify("text", [], [chunk()], source_map={}).citation_precision == 0.0
