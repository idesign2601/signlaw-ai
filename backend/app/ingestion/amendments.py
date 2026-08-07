"""Amendment and version lineage.

The single largest correctness risk in this system. BC municipalities publish a
base bylaw plus a stream of amending bylaws, and some — not all — publish
periodic "consolidated for convenience" versions. Drop several hundred PDFs into
a folder and index them flat, and the retriever will happily return repealed
text and cite it with high confidence. A contractor builds to that spec, and the
citation looks impeccable.

So currency is resolved as a property of the *corpus*, not of a document. This
module runs after every document's metadata is known and answers one question
per document: is this text still the law?

The rules, in order:

1. A bylaw explicitly repealed by another is ``REPEALED``.
2. Where a municipality has consolidated versions of one bylaw, the newest
   consolidation is ``IN_FORCE`` and the older ones are ``SUPERSEDED``.
3. A base bylaw with a later consolidation of itself is ``SUPERSEDED`` — the
   consolidation carries the current text.
4. An amending bylaw is ``SUPERSEDED`` once its changes are folded into a
   consolidation dated after it. Until then it is ``IN_FORCE``, because its text
   is the only place those changes exist.
5. Anything that cannot be placed is ``UNKNOWN`` and surfaced for review. It is
   never assumed current.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from app.core.logging import get_logger
from app.db.enums import DocType, DocumentStatus, RelationType

__all__ = ["DocumentFacts", "LineageResolver", "RelationEdge", "ResolvedDocument"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DocumentFacts:
    """What lineage resolution needs to know about one document."""

    document_id: str
    municipality_slug: str | None
    bylaw_number: str | None
    doc_type: DocType
    consolidation_date: date | None
    effective_date: date | None
    year: int | None
    amends_bylaw_numbers: tuple[str, ...] = ()
    repeals_bylaw_numbers: tuple[str, ...] = ()

    @property
    def version_date(self) -> date | None:
        """Best available date for ordering versions of the same bylaw."""
        if self.consolidation_date:
            return self.consolidation_date
        if self.effective_date:
            return self.effective_date
        if self.year:
            return date(self.year, 1, 1)
        return None


@dataclass(frozen=True, slots=True)
class RelationEdge:
    """A directed lineage edge, ready to persist as a ``bylaw_relation`` row."""

    parent_document_id: str
    child_document_id: str
    relation_type: RelationType
    detected_by: str
    confidence: float
    evidence: str


@dataclass(frozen=True, slots=True)
class ResolvedDocument:
    """The currency verdict for one document."""

    document_id: str
    status: DocumentStatus
    last_amendment_date: date | None
    reason: str
    needs_review: bool = False


@dataclass
class LineageResolver:
    """Resolves in-force status across a whole corpus.

    Deliberately conservative: where the evidence is thin the answer is
    ``UNKNOWN`` and a human is asked, because ``RETRIEVAL__IN_FORCE_ONLY``
    excludes anything not positively established as current.
    """

    facts: Sequence[DocumentFacts]
    _by_number: dict[tuple[str, str], list[DocumentFacts]] = field(
        init=False, repr=False, default_factory=dict
    )

    def __post_init__(self) -> None:
        self._by_number = {}
        for fact in self.facts:
            if fact.municipality_slug and fact.bylaw_number:
                key = (fact.municipality_slug, _normalise_number(fact.bylaw_number))
                self._by_number.setdefault(key, []).append(fact)

    # -- edges ---------------------------------------------------------------

    def build_edges(self) -> list[RelationEdge]:
        """Resolve textual bylaw references into document-to-document edges.

        References are matched only within the same municipality: bylaw numbers
        collide constantly across BC, and a cross-municipality edge would link
        Surrey's 4451 to Coquitlam's.
        """
        edges: list[RelationEdge] = []

        for fact in self.facts:
            if not fact.municipality_slug:
                continue

            for number, relation in (
                *((n, RelationType.AMENDS) for n in fact.amends_bylaw_numbers),
                *((n, RelationType.REPEALS) for n in fact.repeals_bylaw_numbers),
            ):
                targets = self._lookup(fact.municipality_slug, number)
                if not targets:
                    continue
                # An ambiguous reference resolves to nothing rather than to an
                # arbitrary one of several candidates.
                confidence = 0.85 if len(targets) == 1 else 0.4
                for target in targets:
                    if target.document_id == fact.document_id:
                        continue
                    edges.append(
                        RelationEdge(
                            parent_document_id=target.document_id,
                            child_document_id=fact.document_id,
                            relation_type=relation,
                            detected_by="regex:bylaw_reference",
                            confidence=confidence,
                            evidence=f"references bylaw {number}",
                        )
                    )

        edges.extend(self._consolidation_edges())
        return edges

    def _consolidation_edges(self) -> list[RelationEdge]:
        """Link each consolidation to the older versions it replaces."""
        edges: list[RelationEdge] = []

        for versions in self._by_number.values():
            ordered = _order_by_version(versions)
            if len(ordered) < 2:
                continue

            newest = ordered[-1]
            for older in ordered[:-1]:
                edges.append(
                    RelationEdge(
                        parent_document_id=older.document_id,
                        child_document_id=newest.document_id,
                        relation_type=RelationType.CONSOLIDATES
                        if newest.doc_type is DocType.CONSOLIDATED
                        else RelationType.REPLACES,
                        detected_by="version_ordering",
                        confidence=0.75,
                        evidence=(
                            f"newer version dated {newest.version_date} supersedes "
                            f"{older.version_date}"
                        ),
                    )
                )
        return edges

    # -- status --------------------------------------------------------------

    def resolve(self) -> list[ResolvedDocument]:
        """Assign an in-force status to every document."""
        repealed = self._repealed_ids()
        resolved: list[ResolvedDocument] = []

        for fact in self.facts:
            resolved.append(self._resolve_one(fact, repealed))

        counts: dict[str, int] = {}
        for item in resolved:
            counts[item.status.value] = counts.get(item.status.value, 0) + 1
        logger.info("lineage_resolved", documents=len(resolved), **counts)

        return resolved

    def _resolve_one(self, fact: DocumentFacts, repealed: set[str]) -> ResolvedDocument:
        amendment_date = self._last_amendment_date(fact)

        if fact.document_id in repealed:
            return ResolvedDocument(
                fact.document_id,
                DocumentStatus.REPEALED,
                amendment_date,
                "explicitly repealed by a later bylaw",
            )

        if not fact.municipality_slug or not fact.bylaw_number:
            return ResolvedDocument(
                fact.document_id,
                DocumentStatus.UNKNOWN,
                amendment_date,
                "municipality or bylaw number could not be determined",
                needs_review=True,
            )

        siblings = self._lookup(fact.municipality_slug, fact.bylaw_number)
        ordered = _order_by_version(siblings)

        if len(ordered) > 1 and ordered[-1].document_id != fact.document_id:
            return ResolvedDocument(
                fact.document_id,
                DocumentStatus.SUPERSEDED,
                amendment_date,
                f"a newer version dated {ordered[-1].version_date} exists",
            )

        if fact.doc_type is DocType.AMENDMENT:
            return self._resolve_amendment(fact, amendment_date)

        if fact.version_date is None:
            return ResolvedDocument(
                fact.document_id,
                DocumentStatus.UNKNOWN,
                amendment_date,
                "no version date — cannot establish currency",
                needs_review=True,
            )

        return ResolvedDocument(
            fact.document_id,
            DocumentStatus.IN_FORCE,
            amendment_date,
            "newest known version of this bylaw",
        )

    def _resolve_amendment(
        self, fact: DocumentFacts, amendment_date: date | None
    ) -> ResolvedDocument:
        """An amendment is spent once a later consolidation absorbs it."""
        own_date = fact.version_date
        if own_date is None:
            return ResolvedDocument(
                fact.document_id,
                DocumentStatus.UNKNOWN,
                amendment_date,
                "amending bylaw with no date — cannot tell if it is absorbed",
                needs_review=True,
            )

        for target_number in fact.amends_bylaw_numbers:
            for target in self._lookup(fact.municipality_slug or "", target_number):
                if (
                    target.doc_type is DocType.CONSOLIDATED
                    and target.consolidation_date is not None
                    and target.consolidation_date >= own_date
                ):
                    return ResolvedDocument(
                        fact.document_id,
                        DocumentStatus.SUPERSEDED,
                        amendment_date,
                        (f"absorbed into a consolidation dated {target.consolidation_date}"),
                    )

        return ResolvedDocument(
            fact.document_id,
            DocumentStatus.IN_FORCE,
            amendment_date,
            "amendment not yet reflected in any consolidation",
        )

    def _repealed_ids(self) -> set[str]:
        repealed: set[str] = set()
        for fact in self.facts:
            if not fact.municipality_slug:
                continue
            for number in fact.repeals_bylaw_numbers:
                for target in self._lookup(fact.municipality_slug, number):
                    if target.document_id != fact.document_id:
                        repealed.add(target.document_id)
        return repealed

    def _last_amendment_date(self, fact: DocumentFacts) -> date | None:
        """Date of the newest amendment known to affect this document."""
        if not fact.municipality_slug or not fact.bylaw_number:
            return None

        own = _normalise_number(fact.bylaw_number)
        dates = [
            other.version_date
            for other in self.facts
            if other.municipality_slug == fact.municipality_slug
            and other.doc_type is DocType.AMENDMENT
            and any(_normalise_number(n) == own for n in other.amends_bylaw_numbers)
            and other.version_date is not None
        ]
        return max(dates) if dates else None

    def _lookup(self, municipality_slug: str, number: str) -> list[DocumentFacts]:
        return self._by_number.get((municipality_slug, _normalise_number(number)), [])


def _normalise_number(number: str) -> str:
    """Canonical bylaw number for comparison.

    ``4451``, ``4451-2019`` and ``No. 4451`` refer to the same bylaw across
    different municipal citation styles.
    """
    stripped = number.strip().lower().replace("–", "-")
    return stripped.split("-")[0].lstrip("0") or stripped


def _order_by_version(facts: Sequence[DocumentFacts]) -> list[DocumentFacts]:
    """Order versions of one bylaw oldest to newest.

    Dateless documents sort first, so they can never displace a document whose
    currency is actually established.
    """
    return sorted(
        facts,
        key=lambda fact: (
            fact.version_date is not None,
            fact.version_date or date.min,
            fact.doc_type is DocType.CONSOLIDATED,
            fact.document_id,
        ),
    )
