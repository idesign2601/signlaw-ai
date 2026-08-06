"""OCR fallback.

Deliberately conditional. OCR costs seconds to minutes per page and its output
is worse than a real text layer: no reliable section numbering, no font metrics,
no table geometry. Running it across a corpus that mostly has clean text layers
would waste hours and *lower* citation quality.

It runs only when :mod:`app.ingestion.quality` says the text layer is missing or
broken, and only for the pages that failed — a forty-page bylaw with three
scanned pages pays for three.

Trained OCR models are never baked into a Docker image. Tesseract reads them
from ``INGESTION__TESSDATA_DIR`` on a mounted volume, populated by
``make fetch-models``.
"""

from __future__ import annotations

import shutil
import subprocess  # invoking Tesseract is the whole point of this module
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.exceptions import OCRError
from app.core.logging import get_logger
from app.db.enums import ExtractionMethod
from app.domain.models import ExtractedPage
from app.ingestion.quality import assess_page_text

__all__ = ["OcrCapability", "OcrEngine", "OcrResult", "probe_ocr_capability"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OcrCapability:
    """Whether OCR can actually run in this process.

    Checked at startup rather than on first use, so a missing language file
    surfaces as a clear operational message instead of a mid-run failure on
    document 120.
    """

    tesseract_available: bool
    ocrmypdf_available: bool
    available_languages: tuple[str, ...]
    tessdata_dir: Path | None
    detail: str

    @property
    def is_usable(self) -> bool:
        return self.tesseract_available and self.ocrmypdf_available

    def supports(self, languages: str) -> bool:
        """Whether every requested language ('eng+fra') has traineddata."""
        requested = [code for code in languages.split("+") if code]
        return all(code in self.available_languages for code in requested)


def probe_ocr_capability(tessdata_dir: Path | None = None) -> OcrCapability:
    """Report what OCR support is present, without raising."""
    tesseract = shutil.which("tesseract")
    ocrmypdf = shutil.which("ocrmypdf")

    languages: tuple[str, ...] = ()
    if tessdata_dir and tessdata_dir.is_dir():
        languages = tuple(sorted(path.stem for path in tessdata_dir.glob("*.traineddata")))

    if not languages and tesseract:
        # Fall back to Tesseract's own view when TESSDATA_PREFIX is set
        # externally rather than through configuration.
        try:
            completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [tesseract, "--list-langs"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            languages = tuple(
                sorted(line.strip() for line in completed.stdout.splitlines()[1:] if line.strip())
            )
        except (OSError, subprocess.SubprocessError):
            languages = ()

    if not tesseract:
        detail = (
            "tesseract binary not found — install it "
            "(`sudo apt-get install -y tesseract-ocr`), or build the image "
            "with WITH_OCR=true. Pages without a text layer will be skipped."
        )
    elif not ocrmypdf:
        detail = "ocrmypdf not installed — install the backend with the OCR extras"
    elif not languages:
        detail = "no *.traineddata found — run `make fetch-models`"
    else:
        detail = f"ready ({', '.join(languages)})"

    return OcrCapability(
        tesseract_available=bool(tesseract),
        ocrmypdf_available=bool(ocrmypdf),
        available_languages=languages,
        tessdata_dir=tessdata_dir,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Pages recovered by OCR, keyed by page number."""

    pages: dict[int, ExtractedPage]
    ocred_page_numbers: tuple[int, ...]
    skipped: bool = False
    skip_reason: str | None = None


class OcrEngine:
    """Runs OCRmyPDF over the pages that need it.

    Parameters:
        languages: '+'-separated Tesseract language codes.
        dpi: Rasterisation resolution. 300 is the accepted floor for reliable
            small-print recognition; below it, subscript dimensions in bylaw
            tables are routinely misread.
        tessdata_dir: Where traineddata lives, passed to Tesseract via
            ``TESSDATA_PREFIX``.
    """

    def __init__(
        self,
        *,
        languages: str = "eng",
        dpi: int = 300,
        timeout_s: float = 600.0,
        tessdata_dir: Path | None = None,
        min_chars: int = 120,
    ) -> None:
        self.languages = languages
        self.dpi = dpi
        self.timeout_s = timeout_s
        self.tessdata_dir = tessdata_dir
        self.min_chars = min_chars

    def ocr_pages(
        self, path: Path, page_numbers: Sequence[int], *, filename: str | None = None
    ) -> OcrResult:
        """OCR the given pages of a PDF.

        Returns a result rather than raising when OCR is unavailable: a corpus
        with a handful of scanned documents should still index the rest, with
        the scanned ones flagged for review.
        """
        name = filename or path.name

        if not page_numbers:
            return OcrResult(
                pages={}, ocred_page_numbers=(), skipped=True, skip_reason="no pages required OCR"
            )

        capability = probe_ocr_capability(self.tessdata_dir)
        if not capability.is_usable or not capability.supports(self.languages):
            logger.warning(
                "ocr_unavailable",
                filename=name,
                pages=len(page_numbers),
                detail=capability.detail,
            )
            return OcrResult(
                pages={},
                ocred_page_numbers=(),
                skipped=True,
                skip_reason=capability.detail,
            )

        with tempfile.TemporaryDirectory(prefix="signlaw-ocr-") as workdir:
            output = Path(workdir) / "ocr.pdf"
            self._run_ocrmypdf(path, output, page_numbers, name)
            pages = self._read_back(output, page_numbers, name)

        logger.info("ocr_completed", filename=name, pages=len(pages))
        return OcrResult(pages=pages, ocred_page_numbers=tuple(sorted(pages)))

    # -- internals -----------------------------------------------------------

    def _run_ocrmypdf(
        self, source: Path, target: Path, page_numbers: Sequence[int], filename: str
    ) -> None:
        command = [
            "ocrmypdf",
            "--language",
            self.languages,
            "--image-dpi",
            str(self.dpi),
            # Only rasterise and OCR pages without a usable text layer; pages
            # that already have one are passed through untouched.
            "--skip-text",
            "--pages",
            ",".join(str(number) for number in page_numbers),
            # Deskew and clean improve recognition on photocopied bylaws, which
            # are common in older municipal archives.
            "--deskew",
            "--clean",
            "--optimize",
            "1",
            "--quiet",
            str(source),
            str(target),
        ]

        env = None
        if self.tessdata_dir:
            import os

            env = {**os.environ, "TESSDATA_PREFIX": str(self.tessdata_dir)}

        try:
            completed = subprocess.run(  # noqa: S603 — fixed argv, no shell
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise OCRError(
                f"OCR timed out after {self.timeout_s:.0f}s.",
                filename=filename,
                stage="ocr",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise OCRError(
                f"Could not run ocrmypdf: {exc}",
                filename=filename,
                stage="ocr",
                cause=exc,
            ) from exc

        # Exit code 2 means "already has text" for some page selections, which
        # is a benign outcome rather than a failure.
        if completed.returncode not in (0, 2) or not target.exists():
            raise OCRError(
                f"ocrmypdf exited with {completed.returncode}: {completed.stderr.strip()[:400]}",
                filename=filename,
                stage="ocr",
                details={"returncode": completed.returncode},
            )

    def _read_back(
        self, ocred_pdf: Path, page_numbers: Sequence[int], filename: str
    ) -> dict[int, ExtractedPage]:
        """Read the OCR'd text back out of the produced PDF."""
        import fitz

        wanted = set(page_numbers)
        pages: dict[int, ExtractedPage] = {}

        with fitz.open(ocred_pdf) as document:
            for index in range(document.page_count):
                page_number = index + 1
                if page_number not in wanted:
                    continue

                page = document[index]
                text = page.get_text("text")
                report = assess_page_text(text, min_chars=self.min_chars)
                rect = page.rect

                pages[page_number] = ExtractedPage(
                    page_number=page_number,
                    text=text,
                    was_ocred=True,
                    # OCR text is inherently less trustworthy than a real text
                    # layer, so its confidence is capped regardless of how
                    # clean the output looks.
                    ocr_confidence=report.confidence,
                    extraction_method=ExtractionMethod.OCR,
                    extraction_confidence=min(report.confidence, 0.75),
                    width=float(rect.width),
                    height=float(rect.height),
                    rotation=int(page.rotation),
                )

        if not pages:
            logger.warning("ocr_produced_no_text", filename=filename)

        return pages
