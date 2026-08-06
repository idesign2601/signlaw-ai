"""Section-hierarchy parsing.

The heaviest suite in the project, because every citation the system produces
depends on this module attributing text to the right clause. The false-positive
tests matter as much as the positive ones: a measurement misread as a heading
shatters a section and yields citations to clauses that do not exist.
"""

from __future__ import annotations

import pytest

from app.domain.models import TextLine
from app.domain.section_parser import SectionParser, parse_sections


def lines(*texts: str, page: int = 1, font: float | None = None) -> list[TextLine]:
    """Build TextLines with sequential character offsets."""
    result: list[TextLine] = []
    offset = 0
    for text in texts:
        result.append(
            TextLine(
                text=text,
                page_number=page,
                char_start=offset,
                char_end=offset + len(text),
                font_size=font,
            )
        )
        offset += len(text) + 1
    return result


class TestNumberedHeadings:
    def test_simple_numbered_section(self) -> None:
        sections = parse_sections(lines("5. Definitions", "In this Bylaw:"))
        assert len(sections) == 1
        assert sections[0].section_number == "5"
        assert sections[0].heading == "Definitions"

    def test_dotted_section(self) -> None:
        sections = parse_sections(lines("5.3 Fascia Signs", "A fascia sign must not..."))
        assert sections[0].section_number == "5.3"
        assert sections[0].heading == "Fascia Signs"

    def test_three_level_number(self) -> None:
        sections = parse_sections(lines("5.3.1 Maximum Area", "The area shall not exceed."))
        assert sections[0].section_number == "5.3.1"

    def test_deeper_numbers_get_deeper_levels(self) -> None:
        sections = parse_sections(
            lines("5. Signs", "text", "5.3 Fascia", "text", "5.3.1 Area", "text")
        )
        levels = [section.level for section in sections]
        assert levels == sorted(levels)
        assert levels[0] < levels[1] < levels[2]


class TestParts:
    def test_arabic_part(self) -> None:
        sections = parse_sections(lines("PART 5 — SIGN REGULATIONS", "body"))
        assert sections[0].section_number == "Part 5"
        assert sections[0].heading == "SIGN REGULATIONS"

    def test_roman_part_is_converted(self) -> None:
        sections = parse_sections(lines("PART IV — GENERAL", "body"))
        assert sections[0].section_number == "Part 4"

    def test_part_is_the_top_level(self) -> None:
        sections = parse_sections(lines("PART 5 — SIGNS", "body", "5.3 Fascia", "body"))
        assert sections[0].level == 1
        assert sections[1].parent_index == 0

    def test_schedule_is_recognised(self) -> None:
        # Schedules hold the dimension tables, so they must be citable.
        sections = parse_sections(lines("SCHEDULE A — MAXIMUM SIGN AREA", "body"))
        assert sections[0].section_number == "Schedule A"


class TestClauses:
    def test_alpha_clause_nests_under_its_section(self) -> None:
        sections = parse_sections(
            lines("5.3 Fascia Signs", "intro", "(a) must not exceed 20%", "more")
        )
        assert sections[1].section_number == "(a)"
        assert sections[1].parent_index == 0
        assert sections[1].level > sections[0].level

    def test_roman_clause_nests_under_an_alpha_clause(self) -> None:
        sections = parse_sections(
            lines(
                "5.3 Fascia Signs",
                "intro",
                "(a) must not exceed 20%",
                "text",
                "(i) where the building faces a highway",
                "text",
            )
        )
        alpha = sections[1]
        roman = sections[2]
        assert roman.parent_index == alpha.index
        assert roman.level > alpha.level

    def test_clause_level_is_relative_not_absolute(self) -> None:
        shallow = parse_sections(lines("5 Signs", "x", "(a) clause", "y"))
        deep = parse_sections(lines("5.3.1 Signs", "x", "(a) clause", "y"))
        assert shallow[1].level == shallow[0].level + 1
        assert deep[1].level == deep[0].level + 1


class TestFullPath:
    def test_path_accumulates_ancestors(self) -> None:
        sections = parse_sections(
            lines("PART 5 — SIGNS", "x", "5.3 Fascia Signs", "y", "(b) limits", "z")
        )
        assert sections[0].full_path == "Part 5"
        assert sections[1].full_path == "Part 5 > 5.3"
        assert sections[2].full_path == "Part 5 > 5.3 > (b)"

    def test_path_is_available_without_a_recursive_query(self) -> None:
        # Denormalised deliberately: citation rendering is on the hot path.
        sections = parse_sections(lines("5.3 Fascia", "x", "(a) limit", "y"))
        assert all(section.full_path for section in sections)


class TestFalsePositives:
    """Body text that superficially looks like a heading."""

    @pytest.mark.parametrize(
        "text",
        [
            "5.3 metres from the property line",
            "2.5 m in height above grade",
            "10 percent of the total wall area",
            "30 days after the date of issuance",
            "1.2 square metres in area",
        ],
    )
    def test_measurements_are_not_headings(self, text: str) -> None:
        assert parse_sections(lines("5.3 Fascia Signs", text)) == parse_sections(
            lines("5.3 Fascia Signs", text)
        )
        sections = parse_sections(lines("5.3 Fascia Signs", text))
        assert len(sections) == 1

    def test_cross_references_are_not_headings(self) -> None:
        sections = parse_sections(lines("5.3 Fascia Signs", "5.4 section 12 of this Bylaw applies"))
        assert len(sections) == 1

    def test_overlong_lines_are_not_headings(self) -> None:
        long_line = "5.4 " + "a very long sentence that continues at length " * 5
        sections = parse_sections(lines("5.3 Fascia Signs", long_line))
        assert len(sections) == 1

    def test_page_furniture_is_ignored(self) -> None:
        sections = parse_sections(
            lines("5.3 Fascia Signs", "Page 12", "body", "Consolidated to July 2019")
        )
        assert len(sections) == 1

    def test_continuation_lines_are_not_headings(self) -> None:
        sections = parse_sections(lines("5.3 Fascia Signs", "5.4 requirements, and"))
        assert len(sections) == 1


class TestTypographicHeadings:
    def test_larger_font_is_treated_as_a_heading(self) -> None:
        body = lines("regular body text here", font=10.0)
        heading = lines("GENERAL PROVISIONS", font=14.0)
        more = lines("further body text", font=10.0)
        sections = parse_sections([*body, *heading, *more])
        assert any(section.heading == "GENERAL PROVISIONS" for section in sections)

    def test_typographic_headings_have_no_section_number(self) -> None:
        # They organise the document but are not citable clauses.
        content = [
            *lines("body", font=10.0),
            *lines("OVERVIEW", font=15.0),
            *lines("body", font=10.0),
        ]
        sections = parse_sections(content)
        typographic = [s for s in sections if s.heading == "OVERVIEW"]
        assert typographic and typographic[0].section_number == ""

    def test_detection_can_be_disabled_for_ocr(self) -> None:
        # OCR font metrics are noise, so numbering-only is the safer mode.
        content = [*lines("body", font=10.0), *lines("OVERVIEW", font=15.0)]
        assert parse_sections(content, detect_typographic_headings=False) == []

    def test_uniform_font_produces_no_typographic_headings(self) -> None:
        content = lines("line one", "line two", "line three", font=10.0)
        assert parse_sections(content) == []


class TestBodyAttachment:
    def test_body_is_text_up_to_the_next_heading(self) -> None:
        sections = parse_sections(
            lines("5.3 Fascia", "first line", "second line", "5.4 Awning", "other")
        )
        assert "first line" in sections[0].body
        assert "second line" in sections[0].body
        assert "other" not in sections[0].body

    def test_last_section_takes_the_remaining_text(self) -> None:
        sections = parse_sections(lines("5.3 Fascia", "a", "b", "c"))
        assert sections[0].body.splitlines() == ["a", "b", "c"]

    def test_page_range_spans_the_body(self) -> None:
        first = lines("5.3 Fascia", "line on page one", page=1)
        second = lines("continues on page two", page=2)
        # Offsets must stay unique across pages for body attachment to work.
        second = [
            TextLine(
                text=line.text,
                page_number=2,
                char_start=line.char_start + 1000,
                char_end=line.char_end + 1000,
            )
            for line in second
        ]
        sections = parse_sections([*first, *second])
        assert sections[0].page_start == 1
        assert sections[0].page_end == 2


class TestNoHeadings:
    def test_unsectioned_document_returns_empty(self) -> None:
        # Fabricating a section here would let uncitable text be cited as law.
        assert parse_sections(lines("just some prose", "and more prose")) == []

    def test_empty_input(self) -> None:
        assert parse_sections([]) == []

    def test_blank_lines_only(self) -> None:
        assert parse_sections(lines("", "   ", "\t")) == []


class TestOrdinals:
    def test_siblings_are_numbered_in_document_order(self) -> None:
        sections = parse_sections(lines("5.1 One", "x", "5.2 Two", "y", "5.3 Three", "z"))
        assert [section.ordinal for section in sections] == [0, 1, 2]

    def test_ordinals_restart_per_parent(self) -> None:
        sections = parse_sections(
            lines("PART 1 — A", "x", "1.1 One", "y", "PART 2 — B", "z", "2.1 Two", "w")
        )
        by_number = {section.section_number: section for section in sections}
        assert by_number["1.1"].ordinal == 0
        assert by_number["2.1"].ordinal == 0


class TestParserConfiguration:
    def test_font_ratio_threshold_is_respected(self) -> None:
        strict = SectionParser(min_heading_font_ratio=2.0)
        content = [*lines("body", font=10.0), *lines("HEADING", font=12.0)]
        assert strict.parse(content) == []

    def test_lower_threshold_detects_more(self) -> None:
        lenient = SectionParser(min_heading_font_ratio=1.05)
        content = [*lines("body text here", font=10.0), *lines("HEADING", font=11.0)]
        assert lenient.parse(content)
