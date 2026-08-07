"""Reading numeric limits out of bylaw prose.

The highest-risk code in the compliance subsystem: its failure mode is a
plausible number rather than an exception. These tests are mostly about what it
must **refuse** to read.
"""

from __future__ import annotations

import pytest

from app.services.compliance.parsing import extract_limit


class TestReadsLimits:
    def test_shall_not_exceed(self) -> None:
        limit = extract_limit("A fascia sign shall not exceed 9.3 square metres.")
        assert limit is not None
        assert limit.value == 9.3
        assert limit.unit == "m2"

    def test_maximum_height(self) -> None:
        limit = extract_limit("The maximum height of a pylon sign is 7.5 m.")
        assert limit is not None
        assert (limit.value, limit.unit) == (7.5, "m")

    def test_ratio_of_frontage_is_flagged(self) -> None:
        """A ratio is not a limit until frontage is known."""
        limit = extract_limit(
            "Sign area shall not exceed 0.2 square metres per metre of building frontage."
        )
        assert limit is not None
        assert limit.is_ratio_of_frontage is True
        assert limit.value == 0.2

    def test_percentage_of_window_area(self) -> None:
        limit = extract_limit("Window signs shall not exceed 25 percent of the window area.")
        assert limit is not None
        assert (limit.value, limit.unit) == (25.0, "%")

    def test_the_quoted_sentence_is_returned(self) -> None:
        """A number without the text it came from is what this design forbids."""
        limit = extract_limit(
            "Signs are permitted in commercial zones. "
            "A fascia sign shall not exceed 9.3 square metres."
        )
        assert limit is not None
        assert "9.3 square metres" in limit.sentence
        assert "commercial zones" not in limit.sentence


class TestRefusesToGuess:
    def test_deferral_to_a_schedule_yields_nothing(self) -> None:
        """The number nearby belongs to something else.

        "Maximum area as set out in Schedule B" followed by a section number
        would otherwise parse the section number as an area.
        """
        assert extract_limit("Maximum sign area shall be as set out in Schedule B.") is None

    def test_deferral_to_a_section_yields_nothing(self) -> None:
        assert (
            extract_limit(
                "A sign shall not exceed the area specified in Section 4.2 of this Bylaw."
            )
            is None
        )

    def test_a_range_is_not_a_maximum(self) -> None:
        """A range has no single limit to compare against."""
        assert (
            extract_limit(
                "Freestanding signs shall be located between 2.4 and 4.5 metres "
                "from the property line."
            )
            is None
        )

    def test_prose_without_a_limit_yields_nothing(self) -> None:
        assert extract_limit("Signs shall be maintained in good repair.") is None

    def test_empty_text_yields_nothing(self) -> None:
        assert extract_limit("") is None


class TestUnitGuard:
    def test_wrong_unit_is_rejected(self) -> None:
        """A height check must not read the area sentence.

        Without this guard, "shall not exceed 9.3 square metres" would satisfy a
        height rule and the verdict would compare two unrelated quantities.
        """
        assert (
            extract_limit(
                "A fascia sign shall not exceed 9.3 square metres.",
                expected_units=("m",),
            )
            is None
        )

    def test_expected_unit_is_accepted(self) -> None:
        limit = extract_limit("The maximum height is 7.5 metres.", expected_units=("m",))
        assert limit is not None
        assert limit.unit == "m"

    @pytest.mark.parametrize(
        ("text", "unit"),
        [
            ("shall not exceed 100 square feet", "ft2"),
            ("shall not exceed 20 feet", "ft"),
            ("shall not exceed 9.3 m²", "m2"),
            ("shall not exceed 9,3 metres", "m"),
        ],
    )
    def test_unit_spellings(self, text: str, unit: str) -> None:
        """Bylaws are not typographically consistent.

        The comma decimal appears in documents typeset in Quebec and in some
        OCR output.
        """
        limit = extract_limit(text)
        assert limit is not None
        assert limit.unit == unit
