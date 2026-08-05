"""Document metadata detection.

Determines the facts every citation needs: municipality, bylaw title, bylaw
number, version (consolidation) date and amendment date. These live in
inconsistent places across municipal templates — cover page, running header,
footer, filename, or nowhere at all.

Detection runs cheapest-first and each stage only fills what the previous ones
could not:

1. **Filename** — free, and surprisingly reliable across curated corpora.
2. **Regex over the first pages** — the authoritative source when present.
3. **A local LLM pass** — Ollama only, over the first two pages, for the
   fields still missing.

Anything left unresolved or low-confidence is flagged for admin review rather
than guessed. A wrong bylaw number in a citation is worse than a blank one.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.db.enums import DocType, DocumentStatus, MetadataSource
from app.domain.models import DocumentMetadata
from app.domain.municipalities import MunicipalityRegistry

__all__ = ["MetadataDetector", "parse_loose_date"]


# --- patterns ----------------------------------------------------------------

_BYLAW_NUMBER = re.compile(
    r"\bby-?law\s+(?:no\.?|number|#)?\s*(?P<number>\d{1,5}(?:[-–]\d{1,4})?)",
    re.IGNORECASE,
)

_BYLAW_NUMBER_IN_FILENAME = re.compile(
    r"(?:by-?law[_\s-]*)?(?:no[._\s-]*)?(?P<number>\d{3,5}(?:[-–]\d{2,4})?)",
    re.IGNORECASE,
)

_TITLE = re.compile(
    r"(?P<title>"
    r"(?:[A-Z][\w'’.-]*\s+){0,6}"
    r"sign(?:s|age)?\s+(?:regulation\s+)?by-?law"
    r"(?:\s+no\.?\s*\d{1,5}(?:[-–]\d{1,4})?)?"
    r"(?:,?\s*\d{4})?"
    r")",
    re.IGNORECASE,
)

_CONSOLIDATED_TO = re.compile(
    r"consolidated\s+(?:for\s+convenience\s+)?(?:to|as\s+of|up\s+to)\s+"
    r"(?P<date>[A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
    r"\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{4})",
    re.IGNORECASE,
)

_AMENDED_BY = re.compile(
    r"amend(?:ed|ment)\s+(?:by\s+)?(?:by-?law\s+)?(?:no\.?\s*)?"
    r"(?P<number>\d{1,5}(?:[-–]\d{1,4})?)",
    re.IGNORECASE,
)

_AMENDS = re.compile(
    r"\bto\s+amend\s+(?:the\s+)?(?:[\w\s'’-]{0,60}?)by-?law\s+(?:no\.?\s*)?"
    r"(?P<number>\d{1,5}(?:[-–]\d{1,4})?)",
    re.IGNORECASE,
)

_REPEALS = re.compile(
    r"\brepeal(?:s|ed|ing)?\s+(?:the\s+)?(?:[\w\s'’-]{0,60}?)by-?law\s+(?:no\.?\s*)?"
    r"(?P<number>\d{1,5}(?:[-–]\d{1,4})?)",
    re.IGNORECASE,
)

_ADOPTED_ON = re.compile(
    r"(?:adopted|enacted|passed)\s+(?:on\s+|this\s+)?"
    r"(?P<date>[A-Za-z]+\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]+,?\s+\d{4}|"
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2})",
    re.IGNORECASE,
)

_YEAR = re.compile(r"\b(?P<year>19\d{2}|20\d{2})\b")

_DATE_FORMATS = (
    "%B %d, %Y",
    "%B %d %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%B %Y",
    "%b %Y",
)


def parse_loose_date(value: str) -> date | None:
    """Parse the date formats municipal bylaws actually use.

    A bare "July 2019" resolves to the first of the month, because knowing the
    version to within a month is far more useful than discarding it.
    """
    cleaned = re.sub(r"\s+", " ", value).strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()  # noqa: DTZ007 — a calendar date
        except ValueError:
            continue
    return None


@dataclass(frozen=True, slots=True)
class MetadataDetector:
    """Detects document metadata from a filename and the document's text.

    Parameters:
        registry: Municipality master data.
        head_pages: How many leading pages to search. Bylaw identity lives on
            the cover and in the enacting clause; searching the whole document
            adds noise from cross-references to other bylaws.
    """

    registry: MunicipalityRegistry
    head_pages: int = 3

    # -- orchestration -------------------------------------------------------

    def detect(
        self, *, filename: str, page_texts: Sequence[str], pdf_title: str | None = None
    ) -> DocumentMetadata:
        """Run every deterministic stage and merge, highest-trust first."""
        head = "\n".join(page_texts[: self.head_pages])

        from_text = self._from_text(head, pdf_title=pdf_title)
        from_filename = self._from_filename(filename)

        # Document text outranks the filename: a file someone renamed is not
        # evidence, the enacting clause is.
        merged = from_text.merged_with(from_filename)
        return self._score(merged)

    def unresolved_fields(self, metadata: DocumentMetadata) -> tuple[str, ...]:
        """Fields still missing, for the optional local-LLM pass."""
        missing: list[str] = []
        if not metadata.municipality_slug:
            missing.append("municipality")
        if not metadata.title:
            missing.append("title")
        if not metadata.bylaw_number:
            missing.append("bylaw_number")
        if not metadata.year and not metadata.consolidation_date:
            missing.append("year")
        return tuple(missing)

    # -- filename ------------------------------------------------------------

    def _from_filename(self, filename: str) -> DocumentMetadata:
        stem = Path(filename).stem
        readable = re.sub(r"[_\-]+", " ", stem)
        evidence: dict[str, str] = {}

        municipality = None
        candidates = self.registry.find_in_text(readable)
        if len(candidates) == 1:
            municipality = candidates[0]
            evidence["municipality"] = f"filename: {stem}"

        bylaw_number = None
        match = _BYLAW_NUMBER.search(readable) or _BYLAW_NUMBER_IN_FILENAME.search(readable)
        if match:
            candidate = match.group("number")
            # A four-digit number that is a plausible year is far more likely to
            # be the year than the bylaw number.
            if not (len(candidate) == 4 and 1900 <= int(candidate) <= 2100):
                bylaw_number = candidate
                evidence["bylaw_number"] = f"filename: {stem}"

        year = None
        year_match = _YEAR.search(readable)
        if year_match:
            year = int(year_match.group("year"))
            evidence["year"] = f"filename: {stem}"

        return DocumentMetadata(
            municipality_slug=municipality.slug if municipality else None,
            municipality_name=municipality.name if municipality else None,
            bylaw_number=bylaw_number,
            year=year,
            source=MetadataSource.FILENAME,
            confidence=0.4 if evidence else 0.0,
            evidence=evidence,
        )

    # -- document text -------------------------------------------------------

    def _from_text(self, head: str, *, pdf_title: str | None) -> DocumentMetadata:
        if not head.strip():
            return DocumentMetadata()

        evidence: dict[str, str] = {}

        municipality = None
        candidates = self.registry.find_in_text(head)
        if len(candidates) == 1:
            municipality = candidates[0]
            evidence["municipality"] = f"document text: {municipality.official_name}"
        elif len(candidates) > 1:
            # Several municipalities named — usually cross-references. Record it
            # so an operator can see why detection declined to choose.
            evidence["municipality_ambiguous"] = ", ".join(record.name for record in candidates[:5])

        title = None
        title_match = _TITLE.search(head)
        if title_match:
            title = re.sub(r"\s+", " ", title_match.group("title")).strip()
            evidence["title"] = title
        elif pdf_title:
            title = pdf_title
            evidence["title"] = f"PDF metadata: {pdf_title}"

        bylaw_number = None
        number_match = _BYLAW_NUMBER.search(head)
        if number_match:
            bylaw_number = number_match.group("number")
            evidence["bylaw_number"] = number_match.group(0).strip()

        consolidation_date = None
        consolidated_match = _CONSOLIDATED_TO.search(head)
        if consolidated_match:
            consolidation_date = parse_loose_date(consolidated_match.group("date"))
            evidence["consolidation_date"] = consolidated_match.group(0).strip()

        effective = None
        adopted_match = _ADOPTED_ON.search(head)
        if adopted_match:
            effective = parse_loose_date(adopted_match.group("date"))
            evidence["effective_date"] = adopted_match.group(0).strip()

        amends = self._referenced_numbers(head, bylaw_number)
        if amends:
            evidence["amends"] = ", ".join(amends)

        doc_type = self._classify(head, consolidation_date, amends, title)
        year = self._year_for(head, consolidation_date, effective)

        return DocumentMetadata(
            municipality_slug=municipality.slug if municipality else None,
            municipality_name=municipality.name if municipality else None,
            title=title,
            bylaw_number=bylaw_number,
            year=year,
            consolidation_date=consolidation_date,
            doc_type=doc_type,
            # Status is never inferred here. It is resolved only once the whole
            # corpus is known, because "in force" is a statement about a
            # document's relationship to every other document.
            status=DocumentStatus.UNKNOWN,
            source=MetadataSource.REGEX,
            confidence=0.0,
            evidence=evidence,
            amends_bylaw_numbers=amends,
        )

    @staticmethod
    def _referenced_numbers(head: str, own_number: str | None) -> tuple[str, ...]:
        """Bylaw numbers this document acts upon."""
        numbers: list[str] = []
        for pattern in (_AMENDS, _AMENDED_BY, _REPEALS):
            for match in pattern.finditer(head):
                number = match.group("number")
                if number != own_number and number not in numbers:
                    numbers.append(number)
        return tuple(numbers)

    @staticmethod
    def _classify(
        head: str,
        consolidation_date: date | None,
        amends: tuple[str, ...],
        title: str | None,
    ) -> DocType:
        lowered = head.lower()

        if consolidation_date is not None or "consolidated for convenience" in lowered:
            return DocType.CONSOLIDATED
        if _AMENDS.search(head) or re.search(r"\bamendment\s+by-?law\b", lowered):
            return DocType.AMENDMENT
        if amends and not title:
            return DocType.AMENDMENT
        if re.search(r"^\s*schedule\s+[a-z0-9]", lowered, re.MULTILINE):
            return DocType.SCHEDULE
        if title:
            return DocType.BASE
        return DocType.UNKNOWN

    @staticmethod
    def _year_for(head: str, consolidation_date: date | None, effective: date | None) -> int | None:
        if effective:
            return effective.year
        if consolidation_date:
            return consolidation_date.year
        # "Sign Bylaw No. 4451, 2019" — the year attached to the citation.
        titled = re.search(
            r"by-?law\s+no\.?\s*[\d-]+,?\s*(?P<year>19\d{2}|20\d{2})", head, re.IGNORECASE
        )
        if titled:
            return int(titled.group("year"))
        return None

    # -- confidence ----------------------------------------------------------

    @staticmethod
    def _score(metadata: DocumentMetadata) -> DocumentMetadata:
        """Score how much of the citation-critical identity was recovered.

        Weighted by what a citation actually needs. Municipality is worth most:
        without it the document cannot be filtered by city, and answering a
        Burnaby question from a Surrey bylaw is the worst outcome available.
        """
        score = 0.0
        if metadata.municipality_slug:
            score += 0.35
        if metadata.bylaw_number:
            score += 0.25
        if metadata.title:
            score += 0.20
        if metadata.consolidation_date:
            score += 0.12
        elif metadata.year:
            score += 0.08
        if metadata.doc_type is not DocType.UNKNOWN:
            score += 0.08

        if "municipality_ambiguous" in metadata.evidence:
            score = min(score, 0.45)

        return DocumentMetadata(
            municipality_slug=metadata.municipality_slug,
            municipality_name=metadata.municipality_name,
            title=metadata.title,
            bylaw_number=metadata.bylaw_number,
            year=metadata.year,
            consolidation_date=metadata.consolidation_date,
            doc_type=metadata.doc_type,
            status=metadata.status,
            source=metadata.source,
            confidence=round(min(1.0, score), 3),
            evidence=metadata.evidence,
            amends_bylaw_numbers=metadata.amends_bylaw_numbers,
        )
