"""PDF text and layout extraction with PyMuPDF.

Extracts more than plain text, because the downstream section parser needs
layout to work: font size and boldness identify headings in the many bylaws
that mark structure typographically, and bounding boxes let the document viewer
highlight the exact quoted sentence rather than only opening the right page.

Character offsets are assigned continuously across the whole document, so a
section's ``char_start``/``char_end`` locate it unambiguously even when it spans
a page break.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from app.core.logging import get_logger
from app.db.enums import ExtractionMethod
from app.domain.models import ExtractedPage, TextLine
from app.ingestion.quality import assess_page_text

__all__ = ["ExtractionResult", "PdfExtractor"]

logger = get_logger(__name__)

# PyMuPDF span flag bit for bold. Serif/italic live in the same bitfield.
_FLAG_BOLD = 1 << 4


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Everything recovered from one PDF."""

    pages: tuple[ExtractedPage, ...]
    page_count: int
    is_encrypted: bool
    # Pages whose text layer failed the quality gate and need OCR.
    pages_needing_ocr: tuple[int, ...]
    title: str | None = None
    producer: str | None = None

    @property
    def full_text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def all_lines(self) -> tuple[TextLine, ...]:
        return tuple(line for page in self.pages for line in page.lines)

    @property
    def mean_confidence(self) -> float:
        if not self.pages:
            return 0.0
        return sum(page.extraction_confidence for page in self.pages) / len(self.pages)

    @property
    def needs_ocr(self) -> bool:
        return bool(self.pages_needing_ocr)

    @property
    def is_fully_scanned(self) -> bool:
        """Whether every page failed, meaning the PDF has no text layer at all."""
        return bool(self.pages) and len(self.pages_needing_ocr) == len(self.pages)


class PdfExtractor:
    """Extracts text, layout and geometry from a PDF.

    Parameters:
        scan_detection_min_chars: Characters below which a page is treated as
            having no meaningful text layer.
        ocr_confidence_threshold: Quality score below which a page is queued
            for OCR.
    """

    def __init__(
        self,
        *,
        scan_detection_min_chars: int = 120,
        ocr_confidence_threshold: float = 0.5,
    ) -> None:
        self.scan_detection_min_chars = scan_detection_min_chars
        self.ocr_confidence_threshold = ocr_confidence_threshold

    def extract(self, path: Path) -> ExtractionResult:
        """Extract a PDF from disk.

        Raises:
            PDFExtractionError: The file cannot be opened, or is encrypted with
                a password we do not have.
        """
        from app.core.exceptions import PDFExtractionError

        try:
            document = fitz.open(path)
        except Exception as exc:
            raise PDFExtractionError(
                f"Could not open the PDF: {exc}",
                filename=path.name,
                stage="pdf_extract",
                cause=exc,
            ) from exc

        try:
            if document.is_encrypted and not document.authenticate(""):
                raise PDFExtractionError(
                    "The PDF is password protected.",
                    filename=path.name,
                    stage="pdf_extract",
                )

            pages: list[ExtractedPage] = []
            needing_ocr: list[int] = []
            char_offset = 0

            for page_index in range(document.page_count):
                page, char_offset = self._extract_page(
                    document, page_index, char_offset, path.name
                )
                pages.append(page)
                if page.extraction_confidence < self.ocr_confidence_threshold:
                    needing_ocr.append(page.page_number)

            metadata: dict[str, Any] = document.metadata or {}

            result = ExtractionResult(
                pages=tuple(pages),
                page_count=document.page_count,
                is_encrypted=bool(document.is_encrypted),
                pages_needing_ocr=tuple(needing_ocr),
                title=(metadata.get("title") or "").strip() or None,
                producer=(metadata.get("producer") or "").strip() or None,
            )

            logger.info(
                "pdf_extracted",
                filename=path.name,
                pages=result.page_count,
                mean_confidence=round(result.mean_confidence, 3),
                pages_needing_ocr=len(needing_ocr),
            )
            return result
        finally:
            document.close()

    # -- per page ------------------------------------------------------------

    def _extract_page(
        self, document: fitz.Document, page_index: int, char_offset: int, filename: str
    ) -> tuple[ExtractedPage, int]:
        page = document[page_index]
        page_number = page_index + 1

        try:
            raw = page.get_text("dict")
        except Exception as exc:  # one bad page must not kill the document
            logger.warning(
                "page_extraction_failed",
                filename=filename,
                page=page_number,
                error=str(exc),
            )
            return (
                ExtractedPage(
                    page_number=page_number,
                    text="",
                    extraction_method=ExtractionMethod.FAILED,
                    extraction_confidence=0.0,
                ),
                char_offset,
            )

        lines, text, char_offset = self._build_lines(raw, page_number, char_offset)
        report = assess_page_text(text, min_chars=self.scan_detection_min_chars)

        rect = page.rect
        return (
            ExtractedPage(
                page_number=page_number,
                text=text,
                lines=tuple(lines),
                extraction_method=ExtractionMethod.TEXT_LAYER,
                extraction_confidence=report.confidence,
                width=float(rect.width),
                height=float(rect.height),
                rotation=int(page.rotation),
            ),
            char_offset,
        )

    def _build_lines(
        self, raw: dict[str, Any], page_number: int, char_offset: int
    ) -> tuple[list[TextLine], str, int]:
        """Flatten PyMuPDF's block/line/span tree into TextLines.

        Spans within a line are joined; the line takes the font of its longest
        span, because a line reading "**5.3** Fascia Signs" should be judged by
        its dominant typography rather than by whichever span happens to be
        first.
        """
        lines: list[TextLine] = []
        parts: list[str] = []

        for block in raw.get("blocks", []):
            # type 1 blocks are images and carry no text.
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue

                text = "".join(span.get("text", "") for span in spans)
                if not text.strip():
                    continue

                dominant = max(spans, key=lambda span: len(span.get("text", "")))
                bbox = line.get("bbox") or dominant.get("bbox")

                start = char_offset
                end = start + len(text)

                lines.append(
                    TextLine(
                        text=text,
                        page_number=page_number,
                        char_start=start,
                        char_end=end,
                        font_size=float(dominant.get("size", 0.0)) or None,
                        is_bold=bool(int(dominant.get("flags", 0)) & _FLAG_BOLD),
                        indent=float(bbox[0]) if bbox else None,
                        bbox=tuple(float(value) for value in bbox) if bbox else None,  # type: ignore[arg-type]
                    )
                )
                parts.append(text)
                # +1 for the newline joining lines in the page text.
                char_offset = end + 1

        return lines, "\n".join(parts), char_offset
