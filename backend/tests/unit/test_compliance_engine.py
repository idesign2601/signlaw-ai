"""The compliance engine.

The property under test throughout: a verdict is only ever returned alongside
the bylaw text it was computed from, and anything less than a confidently
retrieved rule produces "insufficient information" rather than a pass or a fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.db.enums import ChunkType, DocumentStatus
from app.rag.results import RetrievalTrace, RetrievedChunk
from app.rag.retriever import RetrievalFilters
from app.services.compliance import (
    ComplianceEngine,
    ComplianceOutcome,
    Dimension,
    SignSpec,
    SignType,
)


def _chunk(body: str, *, section: str = "4.2", page: int = 17) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id="chunk-1",
        body=body,
        chunk_type=ChunkType.PROSE,
        document_id="doc-1",
        document_title="Sign Bylaw",
        municipality_slug="burnaby",
        municipality_name="City of Burnaby",
        bylaw_number="6163",
        section_number=section,
        section_path=f"Part 4 > {section}",
        section_heading="Fascia signs",
        page_number=page,
        document_status=DocumentStatus.IN_FORCE,
    )


@dataclass
class _FakeRetriever:
    """Returns scripted chunks and records the filters it was given."""

    chunks: list[RetrievedChunk] = field(default_factory=list)
    calls: list[RetrievalFilters | None] = field(default_factory=list)

    async def retrieve(
        self,
        query: str,
        *,
        filters: RetrievalFilters | None = None,
        top_n: int | None = None,
    ) -> tuple[list[RetrievedChunk], RetrievalTrace]:
        self.calls.append(filters)
        return list(self.chunks), RetrievalTrace(
            query=query,
            collection="test",
            filters={},
            dense_candidates=0,
            sparse_candidates=0,
            fused_candidates=0,
            returned=len(self.chunks),
            reranked=False,
            duration_ms=0,
        )


def _engine(*chunks: RetrievedChunk) -> ComplianceEngine:
    return ComplianceEngine(retriever=_FakeRetriever(chunks=list(chunks)))


class TestVerdicts:
    async def test_within_the_limit_complies(self) -> None:
        engine = _engine(_chunk("A fascia sign shall not exceed 9.3 square metres."))
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=6.0
            )
        )

        area = next(c for c in report.checks if c.dimension is Dimension.AREA)
        assert area.outcome is ComplianceOutcome.COMPLIES
        assert area.limit == 9.3

    async def test_over_the_limit_exceeds(self) -> None:
        engine = _engine(_chunk("A fascia sign shall not exceed 9.3 square metres."))
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=14.0
            )
        )

        assert report.outcome is ComplianceOutcome.EXCEEDS

    async def test_every_verdict_carries_its_citation(self) -> None:
        """The invariant. A number without its source is what this forbids."""
        engine = _engine(_chunk("A fascia sign shall not exceed 9.3 square metres."))
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=6.0
            )
        )

        area = next(c for c in report.checks if c.dimension is Dimension.AREA)
        assert area.evidence is not None
        assert area.evidence.section == "4.2"
        assert area.evidence.page == 17
        assert "9.3 square metres" in area.evidence.quote

    async def test_setback_is_a_minimum_not_a_maximum(self) -> None:
        """Reversing this reports a compliant sign as too close."""
        engine = _engine(
            _chunk("A freestanding sign shall not exceed 3 m from the property line.")
        )
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FREESTANDING,
                municipality_slug="burnaby",
                setback_m=5.0,
            )
        )

        setback = next(c for c in report.checks if c.dimension is Dimension.SETBACK)
        # 5 m clearance against a 3 m minimum is compliant.
        assert setback.outcome is ComplianceOutcome.COMPLIES


class TestRefusesToGuess:
    async def test_nothing_retrieved_is_insufficient_not_compliant(self) -> None:
        report = await _engine().check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=6.0
            )
        )
        assert report.outcome is ComplianceOutcome.INSUFFICIENT_INFORMATION

    async def test_unparseable_rule_still_cites_the_section(self) -> None:
        """A section that defers to a schedule is worth showing the reader."""
        engine = _engine(_chunk("Maximum sign area shall be as set out in Schedule B."))
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=6.0
            )
        )

        area = next(c for c in report.checks if c.dimension is Dimension.AREA)
        assert area.outcome is ComplianceOutcome.INSUFFICIENT_INFORMATION
        assert area.evidence is not None
        assert area.evidence.section == "4.2"

    async def test_ratio_without_frontage_is_insufficient(self) -> None:
        engine = _engine(
            _chunk("Sign area shall not exceed 0.2 square metres per metre of frontage.")
        )
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=6.0
            )
        )

        assert report.outcome is ComplianceOutcome.INSUFFICIENT_INFORMATION
        assert any("frontage" in warning for warning in report.warnings)

    async def test_ratio_with_frontage_resolves(self) -> None:
        engine = _engine(
            _chunk("Sign area shall not exceed 0.2 square metres per metre of frontage.")
        )
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA,
                municipality_slug="burnaby",
                area_sq_m=3.0,
                frontage_m=20.0,
            )
        )

        area = next(c for c in report.checks if c.dimension is Dimension.AREA)
        assert area.limit == 4.0  # 0.2 per metre, over 20 metres of frontage
        assert area.outcome is ComplianceOutcome.COMPLIES

    async def test_one_indeterminate_check_makes_the_report_indeterminate(self) -> None:
        """A sign is not compliant because the checks that ran happened to pass."""
        engine = _engine(_chunk("A fascia sign shall not exceed 9.3 square metres."))
        report = await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA,
                municipality_slug="burnaby",
                area_sq_m=6.0,
                height_m=4.0,  # no height rule in the retrieved text
            )
        )

        assert report.outcome is ComplianceOutcome.INSUFFICIENT_INFORMATION


class TestRetrievalScope:
    async def test_scoped_to_the_municipality_and_in_force_text(self) -> None:
        retriever = _FakeRetriever(
            chunks=[_chunk("A fascia sign shall not exceed 9.3 square metres.")]
        )
        engine = ComplianceEngine(retriever=retriever)

        await engine.check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=6.0
            )
        )

        filters = retriever.calls[0]
        assert filters is not None
        assert filters.municipality_slugs == ("burnaby",)
        assert filters.in_force_only is True

    async def test_ocr_text_is_excluded(self) -> None:
        """A digit misread by OCR is indistinguishable from a correct one.

        Everywhere else OCR provenance is surfaced and left to the reader. Here
        the number is measured against and fabricated from, so it is excluded.
        """
        retriever = _FakeRetriever(
            chunks=[_chunk("A fascia sign shall not exceed 9.3 square metres.")]
        )

        await ComplianceEngine(retriever=retriever).check(
            SignSpec(
                sign_type=SignType.FASCIA, municipality_slug="burnaby", area_sq_m=6.0
            )
        )

        assert retriever.calls[0].exclude_ocr is True
