"""Table extraction.

Sign bylaws express most of their numeric limits as tables — maximum sign area
by zone and sign type, height by street classification, permitted counts by
frontage. "What is the maximum sign area in Vancouver?" is answered by a table,
not by prose.

PyMuPDF's plain text extraction linearises a table into word soup: the
row-to-column association is destroyed, and a model reading the result will
confidently pair the wrong number with the wrong zone. So tables are detected,
lifted out with their structure intact, rendered to markdown, and kept out of
the surrounding prose so the same numbers are not indexed twice in a broken
form.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import fitz

from app.core.logging import get_logger
from app.domain.models import ExtractedPage, ExtractedTable, TextLine

__all__ = ["TableExtractor", "render_markdown"]

logger = get_logger(__name__)

# Overlap fraction above which a text line is considered part of a table and
# removed from the page's prose.
_OVERLAP_THRESHOLD = 0.5


def render_markdown(rows: Sequence[Sequence[str]], headers: Sequence[str] = ()) -> str:
    """Render rows as a markdown table.

    Markdown because it survives embedding and gives the model an unambiguous
    row/column structure in the prompt. Cells are padded to a rectangle so a
    ragged row cannot silently shift values into the wrong column.
    """
    body = [list(row) for row in rows]
    if not body and not headers:
        return ""

    header_row = list(headers) if headers else list(body[0]) if body else []
    data_rows = body[1:] if (not headers and body) else body

    width = max(len(header_row), *(len(row) for row in data_rows), 0)
    if width == 0:
        return ""

    def pad(row: Sequence[str]) -> list[str]:
        cells = [_clean_cell(cell) for cell in row]
        return cells + [""] * (width - len(cells))

    lines = [
        "| " + " | ".join(pad(header_row)) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(pad(row)) + " |" for row in data_rows)
    return "\n".join(lines)


def _clean_cell(value: object) -> str:
    """Collapse newlines and pipes so a cell cannot break the markdown grid."""
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("|", "\\|").strip()


@dataclass(frozen=True, slots=True)
class TableExtractionResult:
    """Tables found on a page, and the prose left after removing them."""

    tables: tuple[ExtractedTable, ...]
    remaining_lines: tuple[TextLine, ...]
    remaining_text: str


class TableExtractor:
    """Finds tables on a page and separates them from prose.

    Parameters:
        min_rows / min_columns: Below these a detection is treated as an
            artefact. Ruled lines, page borders and two-column layouts all
            produce spurious single-row or single-column "tables"; indexing
            those as data pollutes retrieval.
    """

    def __init__(self, *, min_rows: int = 2, min_columns: int = 2) -> None:
        self.min_rows = min_rows
        self.min_columns = min_columns

    def extract_page(
        self, page: fitz.Page, extracted: ExtractedPage, *, filename: str | None = None
    ) -> TableExtractionResult:
        """Extract tables from a rendered page and strip them from the prose."""
        try:
            found = page.find_tables()
        except Exception as exc:  # detection failure is not fatal
            logger.warning(
                "table_detection_failed",
                filename=filename,
                page=extracted.page_number,
                error=str(exc),
            )
            return TableExtractionResult((), extracted.lines, extracted.text)

        tables: list[ExtractedTable] = []
        boxes: list[tuple[float, float, float, float]] = []

        for ordinal, table in enumerate(getattr(found, "tables", [])):
            built = self._build_table(table, extracted.page_number, ordinal)
            if built is None:
                continue
            tables.append(built)
            if built.bbox:
                boxes.append(built.bbox)

        remaining_lines = tuple(
            line for line in extracted.lines if not self._inside_any(line, boxes)
        )
        remaining_text = "\n".join(line.text for line in remaining_lines)

        if tables:
            logger.debug(
                "tables_extracted",
                filename=filename,
                page=extracted.page_number,
                count=len(tables),
            )

        return TableExtractionResult(tuple(tables), remaining_lines, remaining_text)

    # -- internals -----------------------------------------------------------

    def _build_table(self, table: Any, page_number: int, ordinal: int) -> ExtractedTable | None:
        try:
            raw_rows = table.extract()
        except Exception:  # skip a table we cannot read
            return None

        rows = tuple(
            tuple(_clean_cell(cell) for cell in row)
            for row in raw_rows
            if any(_clean_cell(cell) for cell in row)
        )
        if len(rows) < self.min_rows:
            return None
        if max((len(row) for row in rows), default=0) < self.min_columns:
            return None

        headers = self._detect_headers(table, rows)
        bbox = self._bbox(table)

        return ExtractedTable(
            page_number=page_number,
            rows=rows,
            headers=headers,
            markdown=render_markdown(rows, headers),
            bbox=bbox,
            ordinal=ordinal,
        )

    @staticmethod
    def _detect_headers(table: Any, rows: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
        """Identify the header row.

        Prefers PyMuPDF's own header detection; falls back to the first row when
        it looks like labels — non-empty, non-numeric cells. Getting this right
        is what lets a consumer rebuild the grid without guessing.
        """
        header = getattr(table, "header", None)
        names = getattr(header, "names", None) if header is not None else None
        if names:
            cleaned = tuple(_clean_cell(name) for name in names)
            if any(cleaned):
                return cleaned

        if not rows:
            return ()

        first = rows[0]
        populated = [cell for cell in first if cell]
        if not populated:
            return ()
        numeric = sum(1 for cell in populated if _looks_numeric(cell))
        return first if numeric <= len(populated) / 2 else ()

    @staticmethod
    def _bbox(table: Any) -> tuple[float, float, float, float] | None:
        raw = getattr(table, "bbox", None)
        if raw is None:
            return None
        try:
            x0, y0, x1, y1 = (float(value) for value in raw)
        except (TypeError, ValueError):
            return None
        return (x0, y0, x1, y1)

    @staticmethod
    def _inside_any(line: TextLine, boxes: Sequence[tuple[float, float, float, float]]) -> bool:
        """Whether a line sits mostly within one of the table boxes.

        Removing these from the prose stream stops the same numbers being
        indexed twice — once correctly as a table, once as scrambled text.
        """
        if line.bbox is None or not boxes:
            return False

        lx0, ly0, lx1, ly1 = line.bbox
        line_area = max(1e-6, (lx1 - lx0) * (ly1 - ly0))

        for bx0, by0, bx1, by1 in boxes:
            overlap_x = max(0.0, min(lx1, bx1) - max(lx0, bx0))
            overlap_y = max(0.0, min(ly1, by1) - max(ly0, by0))
            if (overlap_x * overlap_y) / line_area >= _OVERLAP_THRESHOLD:
                return True
        return False


def _looks_numeric(value: str) -> bool:
    stripped = value.replace(",", "").replace("%", "").replace("$", "").strip()
    if not stripped:
        return False
    try:
        float(stripped)
    except ValueError:
        return False
    return True
