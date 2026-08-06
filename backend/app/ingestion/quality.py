"""Extraction quality scoring — the gate that decides whether to run OCR.

OCR is slow (seconds to minutes per page) and lossy: its output has no reliable
section numbering, no font metrics and no table geometry. Running it over a
corpus that mostly has clean text layers would waste hours *and* degrade
citation quality. So OCR must be the exception, triggered only by evidence that
the text layer is missing or broken.

Three independent signals, because any one alone misfires:

* **Character density** — a page with almost no extractable text is a scan.
  On its own this misfires on legitimately sparse pages (title pages, tables of
  contents, section dividers).
* **Replacement and control characters** — a page whose text layer decodes to
  mojibake has a broken embedded font. Dense, and completely useless.
* **Word plausibility** — text that extracts as long unbroken character runs
  with no vowels is a failed CID-to-Unicode mapping. Dense, decodes cleanly,
  and still unusable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["QualityReport", "assess_page_text", "should_ocr_page"]

# Unicode replacement char plus the control range that survives bad decoding.
_BAD_CHARS = re.compile(r"[�\x00-\x08\x0b\x0c\x0e-\x1f]")

_WORD = re.compile(r"[A-Za-z]{2,}")
_VOWEL = re.compile(r"[aeiouyAEIOUY]")

# A real English word longer than this without a vowel does not exist; runs like
# "ktrmsfgh" are the signature of a broken font mapping.
_MAX_VOWELLESS_RUN = 6


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Why a page did or did not pass the quality gate."""

    char_count: int
    bad_char_ratio: float
    word_plausibility: float
    confidence: float
    reason: str

    @property
    def is_usable(self) -> bool:
        return self.confidence >= 0.5


def assess_page_text(text: str, *, min_chars: int) -> QualityReport:
    """Score an extracted page from 0.0 (unusable) to 1.0 (clean).

    ``min_chars`` is ``INGESTION__SCAN_DETECTION_MIN_CHARS``: below it, a page
    is treated as having no meaningful text layer.
    """
    stripped = text.strip()
    char_count = len(stripped)

    if char_count == 0:
        return QualityReport(0, 0.0, 0.0, 0.0, "no extractable text — page is an image")

    bad_chars = len(_BAD_CHARS.findall(stripped))
    bad_ratio = bad_chars / char_count

    words = _WORD.findall(stripped)
    plausibility = _word_plausibility(words)

    # Density saturates at 4x the threshold: a page with plenty of text gets no
    # further credit for having even more.
    density = min(1.0, char_count / max(1, min_chars * 4))

    if char_count < min_chars:
        return QualityReport(
            char_count,
            bad_ratio,
            plausibility,
            confidence=round(density * 0.4, 3),
            reason=f"only {char_count} characters extracted (threshold {min_chars})",
        )

    if bad_ratio > 0.05:
        return QualityReport(
            char_count,
            bad_ratio,
            plausibility,
            confidence=round(max(0.0, 0.4 - bad_ratio), 3),
            reason=f"{bad_ratio:.1%} replacement or control characters — broken font",
        )

    if plausibility < 0.5:
        return QualityReport(
            char_count,
            bad_ratio,
            plausibility,
            confidence=round(plausibility * 0.8, 3),
            reason=(
                f"only {plausibility:.0%} of words are plausible — likely a failed character-map"
            ),
        )

    confidence = round(min(1.0, 0.55 + 0.25 * density + 0.20 * plausibility - bad_ratio), 3)
    return QualityReport(char_count, bad_ratio, plausibility, confidence, "text layer is usable")


def _word_plausibility(words: list[str]) -> float:
    """Fraction of extracted words that look like real words."""
    if not words:
        return 0.0

    plausible = 0
    for word in words:
        if len(word) > _MAX_VOWELLESS_RUN and not _VOWEL.search(word):
            continue
        # A single 40-character "word" is a run-together extraction failure.
        if len(word) > 40:
            continue
        plausible += 1

    return plausible / len(words)


def should_ocr_page(text: str, *, min_chars: int, threshold: float = 0.5) -> bool:
    """Whether this page needs OCR.

    Called per page so a document with three scanned pages among forty clean
    ones only pays for the three.
    """
    return assess_page_text(text, min_chars=min_chars).confidence < threshold
