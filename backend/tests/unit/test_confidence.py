"""Confidence scoring.

The property under test throughout: confidence tracks *evidence*, not fluency.
A well-written answer resting on a repealed bylaw must score low, and an answer
with no verified citation must not reach the user as anything but uncertain.
"""

from __future__ import annotations

from app.db.enums import ChunkType, ConfidenceBand, DocumentStatus
from app.domain.confidence import ConfidenceScorer
from app.rag.results import RetrievedChunk


def chunk(chunk_id: str = "c1", **overrides: object) -> RetrievedChunk:
    defaults: dict[str, object] = {
        "chunk_id": chunk_id,
        "body": "A fascia sign must not exceed 20% of the building face.",
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
        "fused_score": 0.9,
    }
    defaults.update(overrides)
    return RetrievedChunk(**defaults)  # type: ignore[arg-type]


def score(scorer: ConfidenceScorer, chunks, cited, **kwargs):
    defaults = {
        "citation_precision": 1.0,
        "uncited_claim_count": 0,
        "municipality_resolved": True,
    }
    defaults.update(kwargs)
    return scorer.score(chunks=chunks, cited_chunk_ids=cited, **defaults)


class TestHighConfidence:
    def test_ideal_case_scores_high(self) -> None:
        # Multiple agreeing sections, current bylaw, exact sections, clean text.
        chunks = [
            chunk("c1", section_number="5.3", fused_score=0.95),
            chunk("c2", section_number="5.4", fused_score=0.4),
            chunk("c3", section_number="5.5", fused_score=0.3),
        ]
        report = score(ConfidenceScorer(), chunks, ["c1", "c2", "c3"])
        assert report.band is ConfidenceBand.HIGH
        assert report.score >= 0.75
        assert not report.warnings

    def test_explanation_is_actionable(self) -> None:
        chunks = [chunk("c1", fused_score=0.95), chunk("c2", section_number="5.4")]
        report = score(ConfidenceScorer(), chunks, ["c1", "c2"])
        assert report.explanation
        assert "confidence" in report.explanation.lower()


class TestStaleDocumentsCapConfidence:
    def test_superseded_citation_caps_the_band(self) -> None:
        # However well everything else scores, this text may not be the law.
        chunks = [
            chunk("c1", document_status=DocumentStatus.SUPERSEDED, fused_score=0.99),
            chunk("c2", document_status=DocumentStatus.SUPERSEDED, section_number="5.4"),
        ]
        report = score(ConfidenceScorer(), chunks, ["c1", "c2"])
        assert report.band in {ConfidenceBand.LOW, ConfidenceBand.INSUFFICIENT}

    def test_repealed_citation_caps_the_band(self) -> None:
        chunks = [chunk("c1", document_status=DocumentStatus.REPEALED)]
        report = score(ConfidenceScorer(), chunks, ["c1"])
        assert report.band in {ConfidenceBand.LOW, ConfidenceBand.INSUFFICIENT}

    def test_warning_names_the_problem(self) -> None:
        chunks = [chunk("c1", document_status=DocumentStatus.SUPERSEDED)]
        report = score(ConfidenceScorer(), chunks, ["c1"])
        assert any("superseded" in warning for warning in report.warnings)

    def test_unknown_status_is_not_treated_as_current(self) -> None:
        current = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        unknown = score(
            ConfidenceScorer(),
            [chunk("c1", document_status=DocumentStatus.UNKNOWN)],
            ["c1"],
        )
        assert unknown.score < current.score


class TestMissingEvidence:
    def test_no_citations_scores_low(self) -> None:
        report = score(
            ConfidenceScorer(), [chunk("c1")], [], citation_precision=0.0
        )
        assert report.band in {ConfidenceBand.LOW, ConfidenceBand.INSUFFICIENT}
        assert any("no verified citation" in w for w in report.warnings)

    def test_abstention_is_insufficient(self) -> None:
        report = ConfidenceScorer().score(
            chunks=(),
            cited_chunk_ids=(),
            citation_precision=0.0,
            uncited_claim_count=0,
            municipality_resolved=False,
            abstained=True,
        )
        assert report.band is ConfidenceBand.INSUFFICIENT
        assert report.score == 0.0

    def test_no_chunks_is_insufficient(self) -> None:
        report = score(ConfidenceScorer(), [], [])
        assert report.band is ConfidenceBand.INSUFFICIENT

    def test_uncited_claims_lower_the_score(self) -> None:
        clean = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        messy = score(
            ConfidenceScorer(), [chunk("c1")], ["c1"], uncited_claim_count=3
        )
        assert messy.score < clean.score
        assert any("without a citation" in w for w in messy.warnings)


class TestMissingMunicipality:
    def test_unresolved_municipality_lowers_confidence(self) -> None:
        resolved = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        unresolved = score(
            ConfidenceScorer(), [chunk("c1")], ["c1"], municipality_resolved=False
        )
        assert unresolved.score < resolved.score

    def test_warning_explains_the_risk(self) -> None:
        report = score(
            ConfidenceScorer(), [chunk("c1")], ["c1"], municipality_resolved=False
        )
        assert any("may not apply to your city" in w for w in report.warnings)

    def test_missing_section_lowers_confidence(self) -> None:
        with_section = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        without = score(
            ConfidenceScorer(), [chunk("c1", section_number=None)], ["c1"]
        )
        assert without.score < with_section.score


class TestConflictingEvidence:
    def test_conflicts_lower_corroboration(self) -> None:
        chunks = [chunk("c1"), chunk("c2", section_number="5.4")]
        agreeing = score(ConfidenceScorer(), chunks, ["c1", "c2"])
        conflicting = score(
            ConfidenceScorer(), chunks, ["c1", "c2"], has_conflicts=True
        )
        assert conflicting.score < agreeing.score
        assert any("disagree" in w for w in conflicting.warnings)


class TestCorroboration:
    def test_more_agreeing_sections_score_higher(self) -> None:
        one = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        three = score(
            ConfidenceScorer(),
            [
                chunk("c1", section_number="5.3"),
                chunk("c2", section_number="5.4"),
                chunk("c3", section_number="5.5"),
            ],
            ["c1", "c2", "c3"],
        )
        assert three.score > one.score


class TestSourceQuality:
    def test_ocr_sources_lower_confidence(self) -> None:
        clean = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        ocred = score(
            ConfidenceScorer(),
            [chunk("c1", from_ocr=True, extraction_confidence=0.7)],
            ["c1"],
        )
        assert ocred.score < clean.score
        assert any("OCR" in w for w in ocred.warnings)


class TestRetrievalMargin:
    def test_flat_scores_are_penalised(self) -> None:
        decisive = score(
            ConfidenceScorer(),
            [chunk("c1", fused_score=0.95), chunk("c2", fused_score=0.10)],
            ["c1"],
        )
        flat = score(
            ConfidenceScorer(),
            [chunk("c1", fused_score=0.50), chunk("c2", fused_score=0.499)],
            ["c1"],
        )
        assert flat.score < decisive.score
        assert any("flat" in w for w in flat.warnings)


class TestReporting:
    def test_factors_are_explainable(self) -> None:
        report = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        payload = report.as_dict()
        assert set(payload["factors"]) == {  # type: ignore[arg-type]
            "citation",
            "currency",
            "corroboration",
            "margin",
            "source_quality",
            "specificity",
        }

    def test_every_factor_carries_a_detail(self) -> None:
        report = score(ConfidenceScorer(), [chunk("c1")], ["c1"])
        assert all(factor.detail for factor in report.factors)

    def test_score_stays_in_range(self) -> None:
        for chunks, cited in (
            ([chunk("c1")], ["c1"]),
            ([chunk("c1", document_status=DocumentStatus.REPEALED)], ["c1"]),
            ([], []),
        ):
            report = score(ConfidenceScorer(), chunks, cited)
            assert 0.0 <= report.score <= 1.0
