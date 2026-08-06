"""Citation preservation through retrieval.

Requirement 4 in full: every retrieved chunk must carry municipality, bylaw,
section, subsection, page, amendment status and source coordinates. These tests
assert each field survives, and that a chunk missing the citation-critical ones
is visibly not citable rather than silently passed to the model.
"""

from __future__ import annotations

from datetime import date

from app.db.enums import ChunkType, DocumentStatus
from app.rag.results import RetrievalTrace, RetrievedChunk, SourceCoordinates


def chunk(**overrides: object) -> RetrievedChunk:
    defaults: dict[str, object] = {
        "chunk_id": "chunk-1",
        "body": "A fascia sign must not exceed 20% of the building face.",
        "chunk_type": ChunkType.PROSE,
        "document_id": "doc-1",
        "document_title": "Sign Bylaw No. 4451, 2019",
        "municipality_slug": "coquitlam",
        "municipality_name": "Coquitlam",
        "bylaw_number": "4451",
        "section_number": "5.3(b)",
        "section_path": "Part 5 > 5.3 > 5.3(b)",
        "section_heading": "Fascia Signs",
        "page_number": 22,
        "document_status": DocumentStatus.IN_FORCE,
    }
    defaults.update(overrides)
    return RetrievedChunk(**defaults)  # type: ignore[arg-type]


class TestRequiredCitationFields:
    def test_municipality(self) -> None:
        assert chunk().municipality_slug == "coquitlam"
        assert chunk().municipality_name == "Coquitlam"

    def test_bylaw(self) -> None:
        assert chunk().bylaw_number == "4451"
        assert chunk().document_title == "Sign Bylaw No. 4451, 2019"

    def test_section_and_subsection_hierarchy(self) -> None:
        result = chunk()
        assert result.section_number == "5.3(b)"
        assert result.section_path == "Part 5 > 5.3 > 5.3(b)"

    def test_page(self) -> None:
        assert chunk().page_number == 22

    def test_amendment_status(self) -> None:
        result = chunk(
            document_status=DocumentStatus.IN_FORCE,
            consolidation_date=date(2021, 7, 15),
            last_amendment_date=date(2020, 5, 1),
        )
        assert result.is_current
        assert result.consolidation_date == date(2021, 7, 15)
        assert result.last_amendment_date == date(2020, 5, 1)

    def test_source_coordinates(self) -> None:
        coordinates = SourceCoordinates(
            x0=72.0, y0=100.0, x1=520.0, y1=140.0, page_width=612.0, page_height=792.0
        )
        assert chunk(coordinates=coordinates).coordinates is coordinates


class TestCitability:
    def test_complete_chunk_is_citable(self) -> None:
        assert chunk().is_citable

    def test_missing_section_is_not_citable(self) -> None:
        # Front matter organises a document but is not an identifiable provision.
        assert not chunk(section_number=None).is_citable

    def test_missing_municipality_is_not_citable(self) -> None:
        assert not chunk(municipality_slug=None).is_citable

    def test_missing_title_is_not_citable(self) -> None:
        assert not chunk(document_title=None).is_citable

    def test_superseded_text_is_not_current(self) -> None:
        assert not chunk(document_status=DocumentStatus.SUPERSEDED).is_current

    def test_unknown_status_is_not_current(self) -> None:
        # Currency is asserted, never assumed.
        assert not chunk(document_status=DocumentStatus.UNKNOWN).is_current


class TestCitationLabel:
    def test_rendered_form(self) -> None:
        assert chunk().citation_label == ("Coquitlam — Sign Bylaw No. 4451, 2019, s. 5.3(b), p. 22")

    def test_falls_back_to_the_page_without_a_section(self) -> None:
        label = chunk(section_number=None).citation_label
        assert "p. 22" in label
        assert "s. None" not in label

    def test_unattributed_document(self) -> None:
        label = chunk(municipality_name=None, document_title=None).citation_label
        assert label.startswith("Unattributed document")


class TestCoordinates:
    def test_ratios_scale_to_the_page(self) -> None:
        coordinates = SourceCoordinates(
            x0=61.2, y0=79.2, x1=306.0, y1=396.0, page_width=612.0, page_height=792.0
        )
        ratios = coordinates.as_ratios()
        assert ratios is not None
        assert ratios[0] == 0.1
        assert ratios[2] == 0.5

    def test_ratios_need_page_geometry(self) -> None:
        assert SourceCoordinates(0, 0, 10, 10).as_ratios() is None


class TestScoring:
    def test_rerank_score_wins_when_present(self) -> None:
        result = chunk(fused_score=0.2).with_rerank_score(0.95)
        assert result.final_score == 0.95

    def test_fused_score_is_used_without_reranking(self) -> None:
        assert chunk(fused_score=0.42).final_score == 0.42

    def test_with_rerank_score_does_not_mutate(self) -> None:
        original = chunk(fused_score=0.2)
        original.with_rerank_score(0.9)
        assert original.rerank_score is None


class TestProvenanceRecord:
    def test_every_audit_field_is_present(self) -> None:
        record = chunk(
            consolidation_date=date(2021, 7, 15),
            last_amendment_date=date(2020, 5, 1),
            from_ocr=True,
        ).provenance()

        for key in (
            "chunk_id",
            "document_id",
            "municipality",
            "bylaw_number",
            "section",
            "section_path",
            "page",
            "status",
            "consolidation_date",
            "last_amendment_date",
            "from_ocr",
        ):
            assert key in record

    def test_dates_serialise_to_iso_strings(self) -> None:
        record = chunk(consolidation_date=date(2021, 7, 15)).provenance()
        assert record["consolidation_date"] == "2021-07-15"

    def test_absent_dates_are_null(self) -> None:
        assert chunk().provenance()["consolidation_date"] is None


class TestTrace:
    def test_trace_records_the_funnel(self) -> None:
        trace = RetrievalTrace(
            query="max sign area",
            collection="signlaw_bge_m3_v1",
            filters={"municipalities": ["coquitlam"]},
            dense_candidates=50,
            sparse_candidates=50,
            fused_candidates=50,
            returned=5,
            reranked=True,
            duration_ms=180,
            chunks=(chunk().provenance(),),
        )
        payload = trace.as_dict()

        assert payload["collection"] == "signlaw_bge_m3_v1"
        assert payload["counts"] == {
            "dense": 50,
            "sparse": 50,
            "fused": 50,
            "returned": 5,
        }
        assert payload["reranked"] is True
        assert len(payload["chunks"]) == 1
