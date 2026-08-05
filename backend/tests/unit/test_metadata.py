"""Metadata detection.

Covers the fields a citation cannot do without: municipality, title, bylaw
number, version date and amendment references. The governing rule is that a
wrong value is worse than a blank one, so every test that asserts a value is
paired with one asserting the detector declines when the evidence is weak.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.db.enums import DocType, DocumentStatus, MetadataSource
from app.domain.municipalities import MunicipalityRegistry
from app.ingestion.metadata import MetadataDetector, parse_loose_date


@pytest.fixture
def detector() -> MetadataDetector:
    return MetadataDetector(registry=MunicipalityRegistry())


COVER_PAGE = """
                        THE CORPORATION OF THE CITY OF COQUITLAM

                             SIGN BYLAW NO. 4451, 2019

                     Consolidated for convenience to July 15, 2021

    A bylaw to regulate the erection and display of signs within the City.
"""


class TestFullCoverPage:
    def test_municipality(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert result.municipality_slug == "coquitlam"

    def test_bylaw_number(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert result.bylaw_number == "4451"

    def test_title(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert result.title is not None
        assert "sign bylaw" in result.title.lower()

    def test_consolidation_date_is_the_version_date(
        self, detector: MetadataDetector
    ) -> None:
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert result.consolidation_date == date(2021, 7, 15)

    def test_classified_as_consolidated(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert result.doc_type is DocType.CONSOLIDATED

    def test_confidence_is_high_and_complete(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert result.is_complete
        assert result.confidence > 0.85
        assert not result.needs_review

    def test_status_is_never_inferred_here(self, detector: MetadataDetector) -> None:
        # Currency is a property of the corpus, resolved by LineageResolver.
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert result.status is DocumentStatus.UNKNOWN


class TestFilenameFallback:
    def test_filename_supplies_what_text_lacks(self, detector: MetadataDetector) -> None:
        result = detector.detect(
            filename="burnaby_sign_bylaw_13743_2020.pdf",
            page_texts=["A bylaw respecting signs."],
        )
        assert result.municipality_slug == "burnaby"
        assert result.bylaw_number == "13743"

    def test_document_text_outranks_the_filename(self, detector: MetadataDetector) -> None:
        # A renamed file is not evidence; the enacting clause is.
        result = detector.detect(
            filename="surrey_bylaw_9999.pdf", page_texts=[COVER_PAGE]
        )
        assert result.municipality_slug == "coquitlam"
        assert result.bylaw_number == "4451"

    def test_year_in_filename_is_not_read_as_a_bylaw_number(
        self, detector: MetadataDetector
    ) -> None:
        result = detector.detect(filename="vancouver_sign_bylaw_2019.pdf", page_texts=[""])
        assert result.bylaw_number is None
        assert result.year == 2019

    def test_source_is_recorded(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="coquitlam_4451.pdf", page_texts=[""])
        assert result.source in {MetadataSource.FILENAME, MetadataSource.REGEX}


class TestAmbiguityIsNotGuessed:
    def test_several_municipalities_named_resolves_to_none(
        self, detector: MetadataDetector
    ) -> None:
        text = "This bylaw is similar to those of Surrey, Richmond and Burnaby."
        result = detector.detect(filename="unknown.pdf", page_texts=[text])
        assert result.municipality_slug is None
        assert "municipality_ambiguous" in result.evidence

    def test_ambiguity_caps_confidence(self, detector: MetadataDetector) -> None:
        text = "Compare Surrey and Richmond sign bylaw no. 1234."
        result = detector.detect(filename="x.pdf", page_texts=[text])
        assert result.confidence <= 0.45
        assert result.needs_review

    def test_bare_langley_does_not_resolve(self, detector: MetadataDetector) -> None:
        result = detector.detect(
            filename="langley_signs.pdf", page_texts=["Langley Sign Bylaw No. 1234"]
        )
        assert result.municipality_slug is None

    def test_qualified_langley_resolves(self, detector: MetadataDetector) -> None:
        result = detector.detect(
            filename="x.pdf", page_texts=["Township of Langley Sign Bylaw No. 1234"]
        )
        assert result.municipality_slug == "langley-township"


class TestAmendments:
    def test_amending_bylaw_is_classified(self, detector: MetadataDetector) -> None:
        text = (
            "CITY OF COQUITLAM\n"
            "BYLAW NO. 4600\n"
            "A bylaw to amend Sign Bylaw No. 4451\n"
        )
        result = detector.detect(filename="4600.pdf", page_texts=[text])
        assert result.doc_type is DocType.AMENDMENT
        assert "4451" in result.amends_bylaw_numbers

    def test_own_number_is_not_listed_as_amended(
        self, detector: MetadataDetector
    ) -> None:
        text = "BYLAW NO. 4600\nA bylaw to amend Sign Bylaw No. 4451"
        result = detector.detect(filename="x.pdf", page_texts=[text])
        assert "4600" not in result.amends_bylaw_numbers

    def test_repeal_reference_is_captured(self, detector: MetadataDetector) -> None:
        text = "City of Surrey Sign Bylaw No. 4451. This bylaw repeals Bylaw No. 3452."
        result = detector.detect(filename="x.pdf", page_texts=[text])
        assert "3452" in result.amends_bylaw_numbers


class TestDateParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("July 15, 2021", date(2021, 7, 15)),
            ("July 15 2021", date(2021, 7, 15)),
            ("Jul 15, 2021", date(2021, 7, 15)),
            ("15 July 2021", date(2021, 7, 15)),
            ("2021-07-15", date(2021, 7, 15)),
            ("2021/07/15", date(2021, 7, 15)),
        ],
    )
    def test_formats(self, raw: str, expected: date) -> None:
        assert parse_loose_date(raw) == expected

    def test_month_only_resolves_to_the_first(self) -> None:
        # Knowing the version to within a month beats discarding it.
        assert parse_loose_date("July 2021") == date(2021, 7, 1)

    def test_unparseable_returns_none(self) -> None:
        assert parse_loose_date("sometime last year") is None

    def test_adopted_date_supplies_the_year(self, detector: MetadataDetector) -> None:
        text = "City of Richmond Sign Bylaw No. 9700\nAdopted on March 3, 2018"
        result = detector.detect(filename="x.pdf", page_texts=[text])
        assert result.year == 2018


class TestUnresolvedFields:
    def test_missing_fields_are_reported(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="scan001.pdf", page_texts=[""])
        missing = detector.unresolved_fields(result)
        assert {"municipality", "title", "bylaw_number"} <= set(missing)

    def test_complete_metadata_has_nothing_missing(
        self, detector: MetadataDetector
    ) -> None:
        result = detector.detect(filename="bylaw.pdf", page_texts=[COVER_PAGE])
        assert detector.unresolved_fields(result) == ()


class TestEmptyInput:
    def test_no_text_and_uninformative_filename(self, detector: MetadataDetector) -> None:
        result = detector.detect(filename="scan001.pdf", page_texts=[])
        assert not result.is_complete
        assert result.needs_review
        assert result.confidence < 0.3

    def test_only_head_pages_are_searched(self, detector: MetadataDetector) -> None:
        # A cross-reference deep in the document must not override the cover.
        pages = [COVER_PAGE, "", "", "City of Surrey Sign Bylaw No. 9999"]
        result = detector.detect(filename="x.pdf", page_texts=pages)
        assert result.municipality_slug == "coquitlam"
        assert result.bylaw_number == "4451"
