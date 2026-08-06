"""Resumable ingestion pipeline.

Processing 500 PDFs takes hours, mostly in OCR. A failure on document 120 must
not restart from zero, and re-running the same folder must not redo work that
already succeeded. Two mechanisms deliver that:

* **Content hashing.** A document is identified by the SHA-256 of its bytes, so
  re-running over the same folder is a no-op for unchanged files.
* **Per-document stage tracking.** Each document records how far it progressed:

      uploaded -> extracted -> ocr_completed -> tables_extracted
               -> metadata_detected -> sections_parsed -> chunked
               -> embedded -> indexed

  A resumed run restarts each document at its own last completed stage. A
  document that failed is retried from the beginning, because partial output
  from a stage that raised cannot be trusted.

One document's failure never stops the run. It is recorded against that
document with the stage and reason, and the pipeline moves on — 499 indexed
bylaws and one flagged failure is a far better outcome than an aborted job.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.core.exceptions import DocumentProcessingError, SignLawError
from app.core.logging import get_logger
from app.db.enums import ExtractionMethod, ProcessingStage
from app.domain.chunking import ChunkingConfig, SectionChunker
from app.domain.models import (
    DocumentMetadata,
    ExtractedPage,
    ExtractedTable,
    ParsedSection,
    TextChunk,
)
from app.domain.municipalities import MunicipalityRegistry
from app.domain.section_parser import SectionParser
from app.ingestion.metadata import MetadataDetector
from app.ingestion.ocr import OcrEngine
from app.ingestion.pdf_extract import PdfExtractor

__all__ = [
    "DocumentOutcome",
    "DocumentPipeline",
    "PipelineConfig",
    "StageRecorder",
    "discover_pdfs",
    "hash_file",
]

logger = get_logger(__name__)

# Read in blocks rather than whole: bylaw PDFs run to hundreds of megabytes and
# a 500-document run would otherwise hold them all in memory.
_HASH_BLOCK_SIZE = 1024 * 1024


def hash_file(path: Path) -> str:
    """SHA-256 of a file's contents — the document's identity."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK_SIZE):
            digest.update(block)
    return digest.hexdigest()


def discover_pdfs(root: Path, *, max_size_bytes: int | None = None) -> Iterator[Path]:
    """Yield PDFs beneath a folder in a stable order.

    Sorted so a resumed run visits documents in the same sequence, which makes
    "it stopped at 120" a reproducible statement.
    """
    if not root.exists():
        raise DocumentProcessingError(
            f"Corpus directory does not exist: {root}", stage="discovery"
        )

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        if path.name.startswith("."):
            continue
        if max_size_bytes is not None and path.stat().st_size > max_size_bytes:
            logger.warning(
                "document_skipped_too_large",
                filename=path.name,
                size_bytes=path.stat().st_size,
                limit_bytes=max_size_bytes,
            )
            continue
        yield path


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Ingestion parameters. Mirrors the ``INGESTION__*`` settings."""

    scan_detection_min_chars: int = 120
    ocr_enabled: bool = True
    ocr_languages: str = "eng"
    ocr_dpi: int = 300
    ocr_timeout_s: float = 600.0
    tessdata_dir: Path | None = None
    ocr_confidence_threshold: float = 0.5
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)
    detect_typographic_headings: bool = True
    head_pages: int = 3


@dataclass
class StageResult:
    """Timing and outcome for one stage."""

    stage: ProcessingStage
    succeeded: bool
    duration_ms: int
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, object] = field(default_factory=dict)


@dataclass
class DocumentOutcome:
    """Everything one document produced, plus how far it got."""

    path: Path
    sha256: str
    stage: ProcessingStage
    pages: tuple[ExtractedPage, ...] = ()
    tables: tuple[ExtractedTable, ...] = ()
    sections: tuple[ParsedSection, ...] = ()
    chunks: tuple[TextChunk, ...] = ()
    metadata: DocumentMetadata = field(default_factory=DocumentMetadata)
    stage_results: list[StageResult] = field(default_factory=list)
    error: SignLawError | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.stage is not ProcessingStage.FAILED

    @property
    def failed_stage(self) -> ProcessingStage | None:
        return next(
            (result.stage for result in self.stage_results if not result.succeeded), None
        )

    @property
    def was_ocred(self) -> bool:
        return any(page.was_ocred for page in self.pages)

    @property
    def mean_extraction_confidence(self) -> float:
        if not self.pages:
            return 0.0
        return sum(page.extraction_confidence for page in self.pages) / len(self.pages)


# Called after each stage so the caller can persist progress. Keeping
# persistence out of the pipeline is what lets it be unit tested without a
# database.
StageRecorder = Callable[[str, StageResult], None]


class DocumentPipeline:
    """Runs one document through the ingestion stages.

    Stateless with respect to the database: it takes a path and a resume point,
    and returns what it produced. The caller owns persistence and job
    bookkeeping.
    """

    def __init__(
        self,
        config: PipelineConfig | None = None,
        *,
        registry: MunicipalityRegistry | None = None,
        recorder: StageRecorder | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.registry = registry or MunicipalityRegistry()
        self.recorder = recorder

        self.extractor = PdfExtractor(
            scan_detection_min_chars=self.config.scan_detection_min_chars,
            ocr_confidence_threshold=self.config.ocr_confidence_threshold,
        )
        self.ocr = OcrEngine(
            languages=self.config.ocr_languages,
            dpi=self.config.ocr_dpi,
            timeout_s=self.config.ocr_timeout_s,
            tessdata_dir=self.config.tessdata_dir,
            min_chars=self.config.scan_detection_min_chars,
        )
        self.section_parser = SectionParser(
            detect_typographic_headings=self.config.detect_typographic_headings
        )
        self.chunker = SectionChunker(self.config.chunking)
        self.detector = MetadataDetector(
            registry=self.registry, head_pages=self.config.head_pages
        )

    def process(
        self, path: Path, *, resume_from: ProcessingStage = ProcessingStage.UPLOADED
    ) -> DocumentOutcome:
        """Process one document, resuming from a given stage.

        Never raises for a per-document failure: the error is recorded on the
        outcome so the run continues.
        """
        outcome = DocumentOutcome(
            path=path, sha256=hash_file(path), stage=ProcessingStage.UPLOADED
        )

        stages: tuple[tuple[ProcessingStage, Callable[[DocumentOutcome], None]], ...] = (
            (ProcessingStage.EXTRACTED, self._stage_extract),
            (ProcessingStage.OCR_COMPLETED, self._stage_ocr),
            (ProcessingStage.TABLES_EXTRACTED, self._stage_tables),
            (ProcessingStage.METADATA_DETECTED, self._stage_metadata),
            (ProcessingStage.SECTIONS_PARSED, self._stage_sections),
            (ProcessingStage.CHUNKED, self._stage_chunk),
        )

        for stage, handler in stages:
            # Everything up to and including the resume point already succeeded
            # in an earlier run.
            if resume_from.is_at_least(stage):
                outcome.stage = stage
                continue

            if not self._run_stage(outcome, stage, handler):
                return outcome

        logger.info(
            "document_processed",
            filename=path.name,
            pages=len(outcome.pages),
            sections=len(outcome.sections),
            chunks=len(outcome.chunks),
            tables=len(outcome.tables),
            ocred=outcome.was_ocred,
            metadata_confidence=outcome.metadata.confidence,
        )
        return outcome

    # -- stage runner --------------------------------------------------------

    def _run_stage(
        self,
        outcome: DocumentOutcome,
        stage: ProcessingStage,
        handler: Callable[[DocumentOutcome], None],
    ) -> bool:
        started = time.perf_counter()
        try:
            handler(outcome)
        except SignLawError as exc:
            return self._record_failure(outcome, stage, started, exc)
        except Exception as exc:  # one document must not kill the run
            wrapped = DocumentProcessingError(
                str(exc),
                filename=outcome.path.name,
                stage=stage.value,
                cause=exc,
            )
            return self._record_failure(outcome, stage, started, wrapped)

        result = StageResult(
            stage=stage,
            succeeded=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        outcome.stage_results.append(result)
        outcome.stage = stage
        self._record(outcome, result)
        return True

    def _record_failure(
        self,
        outcome: DocumentOutcome,
        stage: ProcessingStage,
        started: float,
        error: SignLawError,
    ) -> bool:
        result = StageResult(
            stage=stage,
            succeeded=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_code=error.code,
            error_message=error.message,
            details=dict(error.details),
        )
        outcome.stage_results.append(result)
        outcome.stage = ProcessingStage.FAILED
        outcome.error = error
        self._record(outcome, result)

        logger.warning(
            "document_stage_failed",
            filename=outcome.path.name,
            stage=stage.value,
            error_code=error.code,
            error=error.message,
        )
        return False

    def _record(self, outcome: DocumentOutcome, result: StageResult) -> None:
        if self.recorder is not None:
            self.recorder(outcome.sha256, result)

    # -- stages --------------------------------------------------------------

    def _stage_extract(self, outcome: DocumentOutcome) -> None:
        result = self.extractor.extract(outcome.path)
        outcome.pages = result.pages
        outcome.metadata = outcome.metadata.merged_with(
            DocumentMetadata(title=result.title)
        )

    def _stage_ocr(self, outcome: DocumentOutcome) -> None:
        """OCR only the pages whose text layer failed the quality gate."""
        if not self.config.ocr_enabled:
            return

        needing = tuple(
            page.page_number
            for page in outcome.pages
            if page.extraction_confidence < self.config.ocr_confidence_threshold
        )
        if not needing:
            return

        ocr_result = self.ocr.ocr_pages(
            outcome.path, needing, filename=outcome.path.name
        )
        if ocr_result.skipped:
            return

        outcome.pages = tuple(
            self._prefer_ocr(page, ocr_result.pages.get(page.page_number))
            for page in outcome.pages
        )

    @staticmethod
    def _prefer_ocr(
        original: ExtractedPage, ocred: ExtractedPage | None
    ) -> ExtractedPage:
        """Keep whichever version of a page is actually better.

        OCR output that scores no better than the broken text layer is discarded
        — a bad OCR pass should not replace text that was merely sparse.
        """
        if ocred is None:
            return original
        if ocred.extraction_confidence <= original.extraction_confidence:
            return original
        return ExtractedPage(
            page_number=ocred.page_number,
            text=ocred.text,
            lines=ocred.lines or original.lines,
            tables=original.tables,
            was_ocred=True,
            ocr_confidence=ocred.ocr_confidence,
            extraction_method=(
                ExtractionMethod.MIXED if original.text.strip() else ExtractionMethod.OCR
            ),
            extraction_confidence=ocred.extraction_confidence,
            width=ocred.width or original.width,
            height=ocred.height or original.height,
            rotation=ocred.rotation or original.rotation,
        )

    def _stage_tables(self, outcome: DocumentOutcome) -> None:
        """Lift tables out with their structure, and strip them from the prose.

        Reopens the PDF because table detection needs the rendered page
        geometry, which the extraction stage does not retain.
        """
        import fitz

        from app.ingestion.tables import TableExtractor

        extractor = TableExtractor()
        tables: list[ExtractedTable] = []
        pages: list[ExtractedPage] = []

        with fitz.open(outcome.path) as document:
            for page in outcome.pages:
                # An OCR'd page has no reliable table geometry, so detection is
                # skipped rather than fabricating a grid from noise.
                if page.was_ocred or page.page_number > document.page_count:
                    pages.append(page)
                    continue

                result = extractor.extract_page(
                    document[page.page_number - 1], page, filename=outcome.path.name
                )
                tables.extend(result.tables)
                pages.append(
                    ExtractedPage(
                        page_number=page.page_number,
                        text=result.remaining_text or page.text,
                        lines=result.remaining_lines or page.lines,
                        tables=result.tables,
                        was_ocred=page.was_ocred,
                        ocr_confidence=page.ocr_confidence,
                        extraction_method=page.extraction_method,
                        extraction_confidence=page.extraction_confidence,
                        width=page.width,
                        height=page.height,
                        rotation=page.rotation,
                    )
                )

        outcome.pages = tuple(pages)
        outcome.tables = tuple(tables)

    def _stage_metadata(self, outcome: DocumentOutcome) -> None:
        detected = self.detector.detect(
            filename=outcome.path.name,
            page_texts=[page.text for page in outcome.pages],
            pdf_title=outcome.metadata.title,
        )
        outcome.metadata = detected.merged_with(outcome.metadata)

    def _stage_sections(self, outcome: DocumentOutcome) -> None:
        lines = tuple(line for page in outcome.pages for line in page.lines)
        outcome.sections = tuple(self.section_parser.parse(lines))

        if not outcome.sections:
            logger.warning(
                "no_sections_detected",
                filename=outcome.path.name,
                reason="chunks will carry no section number and cannot be cited by clause",
            )

    def _stage_chunk(self, outcome: DocumentOutcome) -> None:
        fallback = "\n".join(page.text for page in outcome.pages)
        outcome.chunks = tuple(
            self.chunker.chunk(
                outcome.sections,
                tables=outcome.tables,
                fallback_text=fallback,
                fallback_page=outcome.pages[0].page_number if outcome.pages else 1,
            )
        )


def resume_stage_for(recorded: ProcessingStage | None) -> ProcessingStage:
    """Where a document should restart.

    A failed or unknown document restarts from the beginning: partial output
    from a stage that raised cannot be trusted, and re-extracting is cheap
    compared to indexing a half-parsed bylaw.
    """
    if recorded is None or recorded is ProcessingStage.FAILED:
        return ProcessingStage.UPLOADED
    return recorded
