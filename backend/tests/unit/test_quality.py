"""Extraction quality — the gate that decides whether OCR runs.

Two failure modes matter and pull in opposite directions. OCR'ing every PDF
wastes hours and *lowers* citation quality, because OCR output has no reliable
section numbering. Failing to OCR a genuine scan silently drops a bylaw from the
corpus. These tests pin both edges.
"""

from __future__ import annotations

import pytest

from app.ingestion.quality import assess_page_text, should_ocr_page

MIN_CHARS = 120

CLEAN_PAGE = (
    "PART 5 - SIGN REGULATIONS\n"
    "5.3 Fascia Signs\n"
    "(a) A fascia sign must not exceed twenty percent of the area of the "
    "building face to which it is attached.\n"
    "(b) No fascia sign shall project more than 0.3 metres from the building "
    "face, and no sign shall extend above the roof line."
)


class TestCleanText:
    def test_clean_page_scores_high(self) -> None:
        report = assess_page_text(CLEAN_PAGE, min_chars=MIN_CHARS)
        assert report.confidence > 0.8
        assert report.is_usable

    def test_clean_page_is_not_sent_to_ocr(self) -> None:
        assert not should_ocr_page(CLEAN_PAGE, min_chars=MIN_CHARS)

    def test_reason_is_reported(self) -> None:
        assert assess_page_text(CLEAN_PAGE, min_chars=MIN_CHARS).reason


class TestScannedPages:
    def test_empty_page_is_unusable(self) -> None:
        report = assess_page_text("", min_chars=MIN_CHARS)
        assert report.confidence == 0.0
        assert "image" in report.reason

    def test_whitespace_only_page(self) -> None:
        assert assess_page_text("   \n\n  \t ", min_chars=MIN_CHARS).confidence == 0.0

    def test_empty_page_triggers_ocr(self) -> None:
        assert should_ocr_page("", min_chars=MIN_CHARS)

    def test_sparse_page_triggers_ocr(self) -> None:
        # A scan often yields a stray page number from an OCR'd stamp.
        assert should_ocr_page("12", min_chars=MIN_CHARS)

    def test_reason_names_the_threshold(self) -> None:
        report = assess_page_text("a few words only", min_chars=MIN_CHARS)
        assert str(MIN_CHARS) in report.reason


class TestBrokenFonts:
    def test_replacement_characters_lower_confidence(self) -> None:
        broken = "�" * 40 + CLEAN_PAGE
        report = assess_page_text(broken, min_chars=MIN_CHARS)
        assert report.bad_char_ratio > 0.05
        assert not report.is_usable

    def test_broken_font_triggers_ocr_despite_being_dense(self) -> None:
        # Dense but useless: character count alone would pass this.
        broken = "".join("�" for _ in range(400))
        assert should_ocr_page(broken, min_chars=MIN_CHARS)

    def test_control_characters_are_detected(self) -> None:
        text = CLEAN_PAGE + "\x00\x01\x02" * 30
        assert assess_page_text(text, min_chars=MIN_CHARS).bad_char_ratio > 0


class TestFailedCharacterMaps:
    def test_vowelless_runs_are_implausible(self) -> None:
        # The signature of a failed CID-to-Unicode mapping: dense, decodes
        # cleanly, completely unusable.
        garbage = " ".join(["ktrmsfghw", "bcdfghjkl", "mnpqrstvw"] * 20)
        report = assess_page_text(garbage, min_chars=MIN_CHARS)
        assert report.word_plausibility < 0.5
        assert not report.is_usable

    def test_failed_map_triggers_ocr(self) -> None:
        garbage = " ".join(["xzqbcdfg"] * 60)
        assert should_ocr_page(garbage, min_chars=MIN_CHARS)

    def test_run_together_words_are_implausible(self) -> None:
        text = " ".join(["a" * 60] * 10)
        assert assess_page_text(text, min_chars=MIN_CHARS).word_plausibility < 0.5

    def test_real_text_scores_plausible(self) -> None:
        assert assess_page_text(CLEAN_PAGE, min_chars=MIN_CHARS).word_plausibility > 0.9


class TestThresholdBehaviour:
    def test_threshold_is_configurable(self) -> None:
        marginal = "Sign regulations apply. " * 8
        strict = should_ocr_page(marginal, min_chars=MIN_CHARS, threshold=0.99)
        lenient = should_ocr_page(marginal, min_chars=MIN_CHARS, threshold=0.1)
        assert strict and not lenient

    @pytest.mark.parametrize("min_chars", [10, 120, 1000])
    def test_confidence_stays_in_range(self, min_chars: int) -> None:
        for text in ("", "short", CLEAN_PAGE, "�" * 200):
            report = assess_page_text(text, min_chars=min_chars)
            assert 0.0 <= report.confidence <= 1.0

    def test_denser_pages_score_no_lower(self) -> None:
        short = assess_page_text(CLEAN_PAGE, min_chars=MIN_CHARS)
        long = assess_page_text(CLEAN_PAGE * 4, min_chars=MIN_CHARS)
        assert long.confidence >= short.confidence


class TestSelectiveOcr:
    def test_only_bad_pages_are_selected(self) -> None:
        # A forty-page bylaw with three scanned pages must pay for three.
        pages = [CLEAN_PAGE] * 37 + ["", "", "12"]
        needing = [
            index for index, text in enumerate(pages) if should_ocr_page(text, min_chars=MIN_CHARS)
        ]
        assert needing == [37, 38, 39]
