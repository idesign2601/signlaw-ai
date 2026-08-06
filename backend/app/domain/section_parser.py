"""Bylaw section-hierarchy parsing.

This is the citation backbone. Every chunk the system retrieves is attributed to
a section produced here, so an error in this module becomes a wrong legal
citation downstream — the single worst failure mode the product has.

Municipal bylaws are hierarchical but not uniformly formatted::

    PART 5 — SIGN REGULATIONS
      5.1  General
      5.3  Fascia Signs
           (a) A fascia sign must not exceed ...
               (i)  where the building faces a highway ...
    SCHEDULE A — MAXIMUM SIGN AREA BY ZONE

The parser recognises numbering first and falls back to typography (a larger or
bold line followed by body text) for the many bylaws that mark headings visually
rather than numerically. Where neither signal is present it declines to invent
a section, because unattributed text is safer than misattributed text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.models import ParsedSection, TextLine

__all__ = ["SectionParser", "SectionPattern", "parse_sections"]


# -----------------------------------------------------------------------------
# Numbering patterns, most specific first
# -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SectionPattern:
    """A recognised heading form and the hierarchy level it implies."""

    name: str
    regex: re.Pattern[str]
    level: int
    # Levels are recomputed from the number's own depth for dotted forms
    # (5 -> 5.3 -> 5.3.1); fixed levels apply to the rest.
    dynamic_level: bool = False


_ROMAN = "(?:[ivxlcdm]+|[IVXLCDM]+)"

SECTION_PATTERNS: tuple[SectionPattern, ...] = (
    # PART 5 / PART V / PART FIVE — the top of most BC sign bylaws.
    SectionPattern(
        name="part",
        regex=re.compile(
            rf"^(?P<prefix>PART|Part)\s+(?P<number>\d+|{_ROMAN})\b[\s.–—:-]*(?P<heading>.*)$"
        ),
        level=1,
    ),
    # DIVISION 2 — used by larger municipalities beneath Parts.
    SectionPattern(
        name="division",
        regex=re.compile(
            rf"^(?P<prefix>DIVISION|Division)\s+(?P<number>\d+|{_ROMAN})\b[\s.–—:-]*(?P<heading>.*)$"
        ),
        level=2,
    ),
    # SCHEDULE A — where the dimension tables usually live.
    SectionPattern(
        name="schedule",
        regex=re.compile(
            r"^(?P<prefix>SCHEDULE|Schedule|APPENDIX|Appendix)\s+"
            r"(?P<number>[A-Z0-9]{1,3})\b[\s.–—:-]*(?P<heading>.*)$"
        ),
        level=1,
    ),
    # 5.3.1 Heading / 5.3 Heading / 5. Heading — the workhorse form.
    SectionPattern(
        name="numbered",
        regex=re.compile(
            r"^(?P<number>\d{1,3}(?:\.\d{1,3}){0,4})\.?\s+(?P<heading>\S.*)$"
        ),
        level=2,
        dynamic_level=True,
    ),
    # (a) sub-clause
    SectionPattern(
        name="alpha_clause",
        regex=re.compile(r"^\((?P<number>[a-z]{1,2})\)\s+(?P<heading>\S.*)$"),
        level=90,
    ),
    # (i) sub-sub-clause. Checked after (a) so "(i)" reads as roman only when
    # the surrounding context is already alphabetic.
    SectionPattern(
        name="roman_clause",
        regex=re.compile(rf"^\((?P<number>{_ROMAN})\)\s+(?P<heading>\S.*)$"),
        level=91,
    ),
)

# A heading line is rarely long. Anything past this is body text that merely
# happens to begin with a number, e.g. "5.3 metres from the property line".
MAX_HEADING_CHARS = 120

# Lines that look like numbering but are measurements or cross-references.
_MEASUREMENT_TAIL = re.compile(
    r"^\d{1,3}(?:\.\d{1,3})?\s+"
    r"(?:m|mm|cm|metres?|meters?|ft|feet|inches|in|sq|square|percent|%|days?|"
    r"months?|years?|hours?|times?|copies)\b",
    re.IGNORECASE,
)

_CROSS_REFERENCE = re.compile(
    r"^(?:section|subsection|clause|part|schedule)\s", re.IGNORECASE
)

# Repeated page furniture that must not become a section.
_PAGE_FURNITURE = re.compile(
    r"^(?:page\s+\d+|\d+\s*\|\s*page|consolidated\s+to\b|"
    r"city\s+of\s+\w+\s+bylaw\s+no\.?\s*[\d-]+\s*$)",
    re.IGNORECASE,
)


def _roman_to_int(value: str) -> int | None:
    numerals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    lowered = value.lower()
    if not lowered or any(char not in numerals for char in lowered):
        return None

    total = 0
    previous = 0
    for char in reversed(lowered):
        current = numerals[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total or None


class SectionParser:
    """Builds a section tree from extracted text lines.

    Parameters:
        min_heading_font_ratio: How much larger than body text a line must be to
            be treated as a typographic heading. Only consulted when the line
            carries no recognisable numbering.
        detect_typographic_headings: Disable to rely on numbering alone, which
            is the safer setting for OCR output where font metrics are noise.
    """

    def __init__(
        self,
        *,
        min_heading_font_ratio: float = 1.15,
        detect_typographic_headings: bool = True,
    ) -> None:
        self.min_heading_font_ratio = min_heading_font_ratio
        self.detect_typographic_headings = detect_typographic_headings

    # -- public API ----------------------------------------------------------

    def parse(self, lines: Sequence[TextLine]) -> list[ParsedSection]:
        """Parse lines into a flat, parent-linked list of sections.

        Returns document order. An empty list means no headings were recognised;
        callers must treat the document as unsectioned rather than fabricating
        one, so that its chunks are never cited with a section number.
        """
        candidates = self._find_headings(lines)
        if not candidates:
            return []

        sections = self._build_hierarchy(candidates)
        return self._attach_bodies(sections, lines)

    # -- heading detection ---------------------------------------------------

    def _find_headings(self, lines: Sequence[TextLine]) -> list[_Heading]:
        body_font = self._modal_font_size(lines)
        headings: list[_Heading] = []

        for position, line in enumerate(lines):
            if line.is_blank:
                continue

            text = line.stripped
            if _PAGE_FURNITURE.match(text):
                continue

            heading = self._match_numbered(text, line, position)
            if heading is None and self.detect_typographic_headings:
                heading = self._match_typographic(text, line, position, body_font)

            if heading is not None:
                headings.append(heading)

        return headings

    def _match_numbered(self, text: str, line: TextLine, position: int) -> _Heading | None:
        for pattern in SECTION_PATTERNS:
            match = pattern.regex.match(text)
            if match is None:
                continue

            number = match.group("number")
            heading_text = (match.group("heading") or "").strip()

            if pattern.name == "numbered" and not self._is_real_numbered_heading(
                text, heading_text
            ):
                continue

            level = (
                1 + number.count(".") + 1
                if pattern.dynamic_level
                else pattern.level
            )

            return _Heading(
                position=position,
                raw_number=number,
                normalised_number=self._normalise_number(pattern.name, number),
                heading=heading_text or None,
                pattern=pattern.name,
                level=level,
                page_number=line.page_number,
                char_start=line.char_start,
            )
        return None

    def _is_real_numbered_heading(self, text: str, heading_text: str) -> bool:
        """Reject body text that merely starts with a number.

        ``5.3 metres from the property line`` and ``5.3 of this Bylaw`` both
        match the numbering pattern; neither is a heading. Getting this wrong
        shatters a section into fragments and produces citations pointing at
        clauses that do not exist.
        """
        if len(text) > MAX_HEADING_CHARS:
            return False
        if _MEASUREMENT_TAIL.match(text):
            return False
        if _CROSS_REFERENCE.match(heading_text):
            return False
        # A heading ending in a sentence-terminating period followed by more
        # prose is body text.
        return not heading_text.endswith((",", ";", "and", "or"))

    def _match_typographic(
        self, text: str, line: TextLine, position: int, body_font: float | None
    ) -> _Heading | None:
        """Recognise unnumbered headings marked only by font.

        These get an empty section number: they organise the document but cannot
        be cited as a clause, and the empty number is what tells the citation
        layer to fall back to the page reference.
        """
        if body_font is None or line.font_size is None:
            return None
        if len(text) > MAX_HEADING_CHARS or len(text) < 3:
            return None
        if text.endswith((".", ",", ";")):
            return None

        larger = line.font_size >= body_font * self.min_heading_font_ratio
        emphasised = line.is_bold and line.font_size >= body_font
        if not (larger or emphasised):
            return None

        return _Heading(
            position=position,
            raw_number="",
            normalised_number="",
            heading=text,
            pattern="typographic",
            level=2,
            page_number=line.page_number,
            char_start=line.char_start,
        )

    @staticmethod
    def _modal_font_size(lines: Sequence[TextLine]) -> float | None:
        """Most common font size, taken as the body text size."""
        counts: dict[float, int] = {}
        for line in lines:
            if line.is_blank or line.font_size is None:
                continue
            rounded = round(line.font_size, 1)
            counts[rounded] = counts.get(rounded, 0) + len(line.stripped)
        if not counts:
            return None
        return max(counts.items(), key=lambda item: item[1])[0]

    @staticmethod
    def _normalise_number(pattern_name: str, number: str) -> str:
        """Canonical section number for citation rendering."""
        if pattern_name == "part":
            roman = _roman_to_int(number)
            return f"Part {roman if roman and not number.isdigit() else number}"
        if pattern_name == "division":
            roman = _roman_to_int(number)
            return f"Division {roman if roman and not number.isdigit() else number}"
        if pattern_name == "schedule":
            return f"Schedule {number.upper()}"
        if pattern_name in {"alpha_clause", "roman_clause"}:
            return f"({number})"
        return number

    # -- hierarchy -----------------------------------------------------------

    def _build_hierarchy(self, headings: list[_Heading]) -> list[ParsedSection]:
        """Link headings into a tree using a level stack."""
        sections: list[ParsedSection] = []
        # (index, level) of the open ancestors.
        stack: list[tuple[int, int]] = []
        sibling_counts: dict[int | None, int] = {}

        for index, heading in enumerate(headings):
            level = self._effective_level(heading, stack)

            while stack and stack[-1][1] >= level:
                stack.pop()

            parent_index = stack[-1][0] if stack else None
            ordinal = sibling_counts.get(parent_index, 0)
            sibling_counts[parent_index] = ordinal + 1

            full_path = self._full_path(sections, parent_index, heading)

            sections.append(
                ParsedSection(
                    index=index,
                    section_number=heading.normalised_number,
                    heading=heading.heading,
                    level=level,
                    parent_index=parent_index,
                    full_path=full_path,
                    page_start=heading.page_number,
                    page_end=heading.page_number,
                    char_start=heading.char_start,
                    char_end=heading.char_start,
                    ordinal=ordinal,
                )
            )
            stack.append((index, level))

        return sections

    @staticmethod
    def _effective_level(heading: _Heading, stack: list[tuple[int, int]]) -> int:
        """Resolve clause levels relative to their enclosing section.

        ``(a)`` nested under ``5.3`` is one level deeper than ``5.3``, whatever
        absolute depth ``5.3`` happens to sit at.
        """
        if heading.pattern not in {"alpha_clause", "roman_clause"}:
            return heading.level

        base = stack[-1][1] if stack else 2
        return base + (2 if heading.pattern == "roman_clause" else 1)

    @staticmethod
    def _full_path(
        sections: list[ParsedSection], parent_index: int | None, heading: _Heading
    ) -> str:
        """Denormalised ancestor path, e.g. ``Part 5 > 5.3 > (b)``.

        Stored on every section so rendering a citation never needs a recursive
        query on the request path.
        """
        label = heading.normalised_number or heading.heading or "?"
        if parent_index is None:
            return label
        return f"{sections[parent_index].full_path} > {label}"

    # -- body text -----------------------------------------------------------

    def _attach_bodies(
        self, sections: list[ParsedSection], lines: Sequence[TextLine]
    ) -> list[ParsedSection]:
        """Assign each section the text between its heading and the next one."""
        heading_positions = self._heading_positions(sections, lines)
        result: list[ParsedSection] = []

        for index, section in enumerate(sections):
            start = heading_positions[index] + 1
            end = heading_positions[index + 1] if index + 1 < len(sections) else len(lines)

            body_lines = [line for line in lines[start:end] if not line.is_blank]
            body = "\n".join(line.stripped for line in body_lines)

            last_page = body_lines[-1].page_number if body_lines else section.page_start
            char_end = body_lines[-1].char_end if body_lines else section.char_start

            result.append(
                ParsedSection(
                    index=section.index,
                    section_number=section.section_number,
                    heading=section.heading,
                    level=section.level,
                    parent_index=section.parent_index,
                    full_path=section.full_path,
                    page_start=section.page_start,
                    page_end=max(section.page_start, last_page),
                    char_start=section.char_start,
                    char_end=char_end,
                    ordinal=section.ordinal,
                    body=body,
                )
            )

        return result

    @staticmethod
    def _heading_positions(
        sections: list[ParsedSection], lines: Sequence[TextLine]
    ) -> list[int]:
        """Recover each section's line index from its character offset."""
        offsets = {line.char_start: position for position, line in enumerate(lines)}
        return [offsets.get(section.char_start, 0) for section in sections]


@dataclass(frozen=True, slots=True)
class _Heading:
    """A detected heading, before hierarchy resolution."""

    position: int
    raw_number: str
    normalised_number: str
    heading: str | None
    pattern: str
    level: int
    page_number: int
    char_start: int


def parse_sections(
    lines: Sequence[TextLine], *, detect_typographic_headings: bool = True
) -> list[ParsedSection]:
    """Convenience wrapper around :class:`SectionParser`."""
    parser = SectionParser(detect_typographic_headings=detect_typographic_headings)
    return parser.parse(lines)
