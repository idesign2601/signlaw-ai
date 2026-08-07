"""Pure domain value objects.

Frozen dataclasses with no I/O, no ORM and no framework types. The ingestion
pipeline converts PDFs into these, the domain logic transforms them, and the
persistence layer writes them out. Keeping the boundary this sharp is what
makes section parsing, chunking and citation handling testable without a
database, a model server or a single PDF on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Protocol

from app.db.enums import (
    ChunkType,
    DocType,
    DocumentStatus,
    ExtractionMethod,
    MetadataSource,
)

__all__ = [
    "Citation",
    "DocumentMetadata",
    "ExtractedPage",
    "ExtractedTable",
    "ParsedSection",
    "SectionRef",
    "TextChunk",
    "TextLine",
    "TokenCounter",
    "estimate_tokens",
]


class TokenCounter(Protocol):
    """Counts tokens in a string.

    Injected rather than imported so the domain layer stays free of tokenizer
    dependencies, and so chunk sizing can be matched to whichever model is
    configured.
    """

    def __call__(self, text: str) -> int: ...


# Empirically ~3.6 characters per token for English legal prose across GPT-4o,
# Qwen 2.5 and Llama 3 tokenizers. Deliberately slightly pessimistic: chunks
# that come in a little under target are harmless, chunks that overflow the
# model's context are not.
_CHARS_PER_TOKEN = 3.6


def estimate_tokens(text: str) -> int:
    """Estimate a token count without loading a tokenizer.

    The default :class:`TokenCounter`. Accurate to roughly ±10% on bylaw prose,
    which is well inside the slack between the target and maximum chunk size.
    Inject a real tokenizer where exactness matters.
    """
    if not text:
        return 0
    return max(1, round(len(text) / _CHARS_PER_TOKEN))


@dataclass(frozen=True, slots=True)
class TextLine:
    """One line of extracted text, with the layout signals needed for parsing.

    Font size and boldness come from PyMuPDF spans. They matter because many
    municipal bylaws mark headings typographically rather than with consistent
    numbering, and numbering alone misses those headings.
    """

    text: str
    page_number: int
    char_start: int
    char_end: int
    font_size: float | None = None
    is_bold: bool = False
    # Distance from the left text margin, in points. Sub-clause numbering is
    # often expressed purely as indentation.
    indent: float | None = None
    # Source coordinates on the page in PDF points (x0, y0, x1, y1). Carried
    # through so the document viewer can highlight the exact quoted text rather
    # than only jumping to the page.
    bbox: tuple[float, float, float, float] | None = None

    @property
    def stripped(self) -> str:
        return self.text.strip()

    @property
    def is_blank(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    """A table lifted out of a page with its structure intact.

    Sign bylaws express most numeric limits as tables — maximum area by zone and
    sign type, height by street classification. Linearised into prose, the
    row/column association is lost and the model will confidently pair the wrong
    number with the wrong zone. Tables therefore travel as markdown and are never
    split across chunks.
    """

    page_number: int
    rows: tuple[tuple[str, ...], ...]
    markdown: str
    # First row when it is a header row. Kept separate from `rows` so a
    # consumer can rebuild the grid without guessing which row is the header.
    headers: tuple[str, ...] = ()
    caption: str | None = None
    # Bounding box in PDF points, used to excise the table from the page's prose
    # and to locate it when rendering the source page.
    bbox: tuple[float, float, float, float] | None = None
    ordinal: int = 0

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def body_rows(self) -> tuple[tuple[str, ...], ...]:
        """Rows excluding the header row, if one was identified."""
        if self.headers and self.rows and tuple(self.rows[0]) == self.headers:
            return self.rows[1:]
        return self.rows

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def is_degenerate(self) -> bool:
        """Whether this is too small or too empty to be a real table.

        Table detectors routinely emit single-column artefacts from ruled lines
        and page borders. Treating those as tables pollutes retrieval.
        """
        if self.row_count < 2 or self.column_count < 2:
            return True
        populated = sum(1 for row in self.rows for cell in row if cell.strip())
        return populated < (self.row_count * self.column_count) * 0.3


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    """Everything recovered from a single PDF page."""

    page_number: int
    text: str
    lines: tuple[TextLine, ...] = ()
    tables: tuple[ExtractedTable, ...] = ()
    was_ocred: bool = False
    ocr_confidence: float | None = None
    extraction_method: ExtractionMethod = ExtractionMethod.TEXT_LAYER
    # 0.0-1.0. See app.ingestion.quality for how this is computed.
    extraction_confidence: float = 1.0
    # Page geometry in PDF points, for mapping a bbox onto a rendered page.
    width: float | None = None
    height: float | None = None
    rotation: int = 0

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def has_tables(self) -> bool:
        return any(not table.is_degenerate for table in self.tables)

    def looks_scanned(self, min_chars: int) -> bool:
        """Whether this page carries too little text to be a real text layer."""
        return self.char_count < min_chars


@dataclass(frozen=True, slots=True)
class SectionRef:
    """A citable location inside a document.

    The minimum a retrieved chunk must carry for its citation to be verifiable.
    """

    section_number: str
    full_path: str
    heading: str | None
    level: int
    page_start: int
    page_end: int

    def __str__(self) -> str:
        return f"s. {self.section_number}" if self.section_number else self.full_path


@dataclass(frozen=True, slots=True)
class ParsedSection:
    """A node in a document's section hierarchy.

    ``parent_index`` refers to a position in the flat list the parser returns.
    Indices rather than object references keep the structure trivially
    serialisable and free of cycles.
    """

    index: int
    section_number: str
    heading: str | None
    level: int
    parent_index: int | None
    full_path: str
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    ordinal: int
    body: str = ""

    @property
    def ref(self) -> SectionRef:
        return SectionRef(
            section_number=self.section_number,
            full_path=self.full_path,
            heading=self.heading,
            level=self.level,
            page_start=self.page_start,
            page_end=self.page_end,
        )

    def with_body(self, body: str) -> ParsedSection:
        return replace(self, body=body)


@dataclass(frozen=True, slots=True)
class TextChunk:
    """A retrievable unit of text that always knows where it came from.

    Chunks never cross a section boundary, so :attr:`section` is always the
    correct citation for :attr:`body` — that invariant is the whole reason the
    chunker is structure-aware rather than fixed-size.
    """

    body: str
    page_start: int
    page_end: int
    token_count: int
    chunk_type: ChunkType = ChunkType.PROSE
    section: SectionRef | None = None
    ordinal: int = 0
    # Set on child chunks produced by splitting an oversized section; points at
    # the whole-section parent used for small-to-big retrieval.
    parent_ordinal: int | None = None

    @property
    def is_citable(self) -> bool:
        """Whether this chunk can support a citation with a section number.

        Front matter and text preceding the first recognised heading cannot, and
        must be confidence-penalised rather than cited as though it were law.
        """
        return self.section is not None and bool(self.section.section_number)


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Bibliographic facts about a source PDF.

    Every field carries the evidence and confidence behind it, because these are
    inferred from inconsistent municipal templates rather than read from a
    reliable header. Low-confidence values go to the admin review queue instead
    of silently becoming part of a citation.
    """

    municipality_slug: str | None = None
    municipality_name: str | None = None
    title: str | None = None
    bylaw_number: str | None = None
    year: int | None = None
    consolidation_date: date | None = None
    doc_type: DocType = DocType.UNKNOWN
    status: DocumentStatus = DocumentStatus.UNKNOWN
    source: MetadataSource | None = None
    confidence: float = 0.0
    # Field name -> the text that produced the value, for admin review.
    evidence: dict[str, str] = field(default_factory=dict)
    # Bylaw numbers this document amends, consolidates or repeals. Resolved to
    # document identifiers once the whole corpus has been ingested.
    amends_bylaw_numbers: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """Whether enough was determined to index the document confidently."""
        return bool(self.municipality_slug and self.title and self.bylaw_number)

    @property
    def needs_review(self) -> bool:
        return not self.is_complete or self.confidence < 0.6

    def merged_with(self, other: DocumentMetadata) -> DocumentMetadata:
        """Fill this record's gaps from ``other`` without overwriting anything.

        Detection runs cheapest-first — filename, then regex, then an LLM pass —
        and each stage only fills what the previous stages could not determine.
        """
        merged_evidence = {**other.evidence, **self.evidence}
        return DocumentMetadata(
            municipality_slug=self.municipality_slug or other.municipality_slug,
            municipality_name=self.municipality_name or other.municipality_name,
            title=self.title or other.title,
            bylaw_number=self.bylaw_number or other.bylaw_number,
            year=self.year or other.year,
            consolidation_date=self.consolidation_date or other.consolidation_date,
            doc_type=(self.doc_type if self.doc_type is not DocType.UNKNOWN else other.doc_type),
            status=(self.status if self.status is not DocumentStatus.UNKNOWN else other.status),
            source=self.source or other.source,
            confidence=max(self.confidence, other.confidence),
            evidence=merged_evidence,
            amends_bylaw_numbers=tuple(
                dict.fromkeys((*self.amends_bylaw_numbers, *other.amends_bylaw_numbers))
            ),
        )


_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Citation:
    """A single verifiable reference attached to an answer.

    ``quote`` is what makes the citation auditable rather than decorative: the
    reader can check the claim against the source in one click, and the
    verification pass can confirm the quote actually appears in retrieved text.
    """

    document_id: str
    document_title: str
    municipality: str
    page: int
    quote: str
    bylaw_number: str | None = None
    section_number: str | None = None
    section_path: str | None = None
    relevance: float = 0.0
    # True when the source page came from OCR, which weakens the citation.
    from_ocr: bool = False

    def render(self) -> str:
        """Human-readable citation, e.g.
        ``Coquitlam — Sign Bylaw No. 4451, s. 5.3(b), p. 22``."""
        parts = [self.municipality, self.document_title]
        if self.section_number:
            parts.append(f"s. {self.section_number}")
        parts.append(f"p. {self.page}")
        return " — ".join(parts[:2]) + ", " + ", ".join(parts[2:])

    def supports(self, source_text: str) -> bool:
        """Whether the quote actually appears in the given retrieved text.

        Compared on collapsed whitespace because PDF extraction introduces line
        breaks and runs of spaces that are meaningless to the comparison.
        """
        if not self.quote.strip():
            return False
        needle = _WHITESPACE.sub(" ", self.quote).strip().casefold()
        haystack = _WHITESPACE.sub(" ", source_text).casefold()
        return needle in haystack

    @property
    def deep_link(self) -> str:
        """Path the frontend uses to open the source page with the quote lit up."""
        return f"/documents/{self.document_id}/page/{self.page}"
