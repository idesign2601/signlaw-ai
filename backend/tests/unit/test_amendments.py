"""Amendment and version lineage.

The rule under test throughout: nothing is `IN_FORCE` unless the corpus
positively establishes it. Everything else is `SUPERSEDED`, `REPEALED`, or
`UNKNOWN` and flagged — because `RETRIEVAL__IN_FORCE_ONLY` excludes anything
not established as current, and a confidently-cited repealed clause is the
worst output this system can produce.
"""

from __future__ import annotations

from datetime import date

from app.db.enums import DocType, DocumentStatus, RelationType
from app.ingestion.amendments import DocumentFacts, LineageResolver


def facts(
    document_id: str,
    *,
    number: str | None = "4451",
    municipality: str | None = "coquitlam",
    doc_type: DocType = DocType.BASE,
    consolidation: date | None = None,
    effective: date | None = None,
    year: int | None = None,
    amends: tuple[str, ...] = (),
    repeals: tuple[str, ...] = (),
) -> DocumentFacts:
    return DocumentFacts(
        document_id=document_id,
        municipality_slug=municipality,
        bylaw_number=number,
        doc_type=doc_type,
        consolidation_date=consolidation,
        effective_date=effective,
        year=year,
        amends_bylaw_numbers=amends,
        repeals_bylaw_numbers=repeals,
    )


def status_of(resolved: list, document_id: str) -> DocumentStatus:
    return next(item.status for item in resolved if item.document_id == document_id)


class TestNewestVersionWins:
    def test_older_consolidation_is_superseded(self) -> None:
        resolved = LineageResolver(
            [
                facts("old", doc_type=DocType.CONSOLIDATED, consolidation=date(2015, 1, 1)),
                facts("new", doc_type=DocType.CONSOLIDATED, consolidation=date(2021, 6, 1)),
            ]
        ).resolve()

        assert status_of(resolved, "old") is DocumentStatus.SUPERSEDED
        assert status_of(resolved, "new") is DocumentStatus.IN_FORCE

    def test_base_bylaw_superseded_by_its_consolidation(self) -> None:
        resolved = LineageResolver(
            [
                facts("base", effective=date(1998, 3, 1)),
                facts(
                    "consolidated",
                    doc_type=DocType.CONSOLIDATED,
                    consolidation=date(2019, 7, 1),
                ),
            ]
        ).resolve()

        assert status_of(resolved, "base") is DocumentStatus.SUPERSEDED
        assert status_of(resolved, "consolidated") is DocumentStatus.IN_FORCE

    def test_single_version_is_in_force(self) -> None:
        resolved = LineageResolver([facts("only", effective=date(2019, 1, 1))]).resolve()
        assert status_of(resolved, "only") is DocumentStatus.IN_FORCE


class TestRepeal:
    def test_explicitly_repealed_bylaw(self) -> None:
        resolved = LineageResolver(
            [
                facts("old", number="3452", effective=date(1998, 1, 1)),
                facts(
                    "new",
                    number="4451",
                    effective=date(2019, 1, 1),
                    repeals=("3452",),
                ),
            ]
        ).resolve()

        assert status_of(resolved, "old") is DocumentStatus.REPEALED
        assert status_of(resolved, "new") is DocumentStatus.IN_FORCE

    def test_repeal_does_not_cross_municipalities(self) -> None:
        # Bylaw numbers collide constantly across BC.
        resolved = LineageResolver(
            [
                facts(
                    "surrey-3452", number="3452", municipality="surrey", effective=date(1998, 1, 1)
                ),
                facts(
                    "coq-4451",
                    number="4451",
                    municipality="coquitlam",
                    effective=date(2019, 1, 1),
                    repeals=("3452",),
                ),
            ]
        ).resolve()

        assert status_of(resolved, "surrey-3452") is not DocumentStatus.REPEALED


class TestAmendments:
    def test_amendment_absorbed_by_a_later_consolidation(self) -> None:
        resolved = LineageResolver(
            [
                facts(
                    "consolidated",
                    number="4451",
                    doc_type=DocType.CONSOLIDATED,
                    consolidation=date(2021, 1, 1),
                ),
                facts(
                    "amend",
                    number="4600",
                    doc_type=DocType.AMENDMENT,
                    effective=date(2020, 5, 1),
                    amends=("4451",),
                ),
            ]
        ).resolve()

        assert status_of(resolved, "amend") is DocumentStatus.SUPERSEDED

    def test_amendment_newer_than_the_consolidation_stays_in_force(self) -> None:
        # Its text is the only place those changes exist.
        resolved = LineageResolver(
            [
                facts(
                    "consolidated",
                    number="4451",
                    doc_type=DocType.CONSOLIDATED,
                    consolidation=date(2019, 1, 1),
                ),
                facts(
                    "amend",
                    number="4600",
                    doc_type=DocType.AMENDMENT,
                    effective=date(2022, 5, 1),
                    amends=("4451",),
                ),
            ]
        ).resolve()

        assert status_of(resolved, "amend") is DocumentStatus.IN_FORCE

    def test_last_amendment_date_is_recorded(self) -> None:
        resolved = LineageResolver(
            [
                facts("base", number="4451", effective=date(2015, 1, 1)),
                facts(
                    "a1",
                    number="4600",
                    doc_type=DocType.AMENDMENT,
                    effective=date(2018, 3, 1),
                    amends=("4451",),
                ),
                facts(
                    "a2",
                    number="4700",
                    doc_type=DocType.AMENDMENT,
                    effective=date(2021, 9, 1),
                    amends=("4451",),
                ),
            ]
        ).resolve()

        base = next(item for item in resolved if item.document_id == "base")
        assert base.last_amendment_date == date(2021, 9, 1)


class TestConservativeDefaults:
    def test_missing_municipality_is_unknown_and_flagged(self) -> None:
        resolved = LineageResolver([facts("doc", municipality=None)]).resolve()
        assert status_of(resolved, "doc") is DocumentStatus.UNKNOWN
        assert resolved[0].needs_review

    def test_missing_bylaw_number_is_unknown(self) -> None:
        resolved = LineageResolver([facts("doc", number=None)]).resolve()
        assert status_of(resolved, "doc") is DocumentStatus.UNKNOWN

    def test_no_date_is_unknown_not_in_force(self) -> None:
        # Currency cannot be established, so it is not asserted.
        resolved = LineageResolver([facts("doc", year=None)]).resolve()
        assert status_of(resolved, "doc") is DocumentStatus.UNKNOWN
        assert resolved[0].needs_review

    def test_undated_version_never_displaces_a_dated_one(self) -> None:
        resolved = LineageResolver(
            [
                facts("dated", consolidation=date(2020, 1, 1), doc_type=DocType.CONSOLIDATED),
                facts("undated", year=None),
            ]
        ).resolve()
        assert status_of(resolved, "dated") is DocumentStatus.IN_FORCE


class TestNumberNormalisation:
    def test_hyphenated_and_bare_numbers_match(self) -> None:
        resolved = LineageResolver(
            [
                facts("base", number="4451", effective=date(2010, 1, 1)),
                facts(
                    "amend",
                    number="4600",
                    doc_type=DocType.AMENDMENT,
                    effective=date(2020, 1, 1),
                    amends=("4451-2019",),
                ),
            ]
        ).resolve()
        base = next(item for item in resolved if item.document_id == "base")
        assert base.last_amendment_date == date(2020, 1, 1)

    def test_leading_zeros_are_ignored(self) -> None:
        resolver = LineageResolver(
            [
                facts("base", number="0451", effective=date(2010, 1, 1)),
                facts(
                    "amend",
                    number="4600",
                    doc_type=DocType.AMENDMENT,
                    effective=date(2020, 1, 1),
                    amends=("451",),
                ),
            ]
        )
        assert any(edge.relation_type is RelationType.AMENDS for edge in resolver.build_edges())


class TestEdges:
    def test_amendment_edge_is_built(self) -> None:
        edges = LineageResolver(
            [
                facts("base", number="4451", effective=date(2010, 1, 1)),
                facts(
                    "amend",
                    number="4600",
                    doc_type=DocType.AMENDMENT,
                    effective=date(2020, 1, 1),
                    amends=("4451",),
                ),
            ]
        ).build_edges()

        amends = [e for e in edges if e.relation_type is RelationType.AMENDS]
        assert len(amends) == 1
        assert amends[0].parent_document_id == "base"
        assert amends[0].child_document_id == "amend"

    def test_dangling_reference_produces_no_edge(self) -> None:
        # A bylaw referencing a document not in the corpus.
        edges = LineageResolver(
            [facts("amend", number="4600", doc_type=DocType.AMENDMENT, amends=("9999",))]
        ).build_edges()
        assert not [e for e in edges if e.relation_type is RelationType.AMENDS]

    def test_no_self_edges(self) -> None:
        edges = LineageResolver([facts("doc", number="4451", amends=("4451",))]).build_edges()
        assert all(e.parent_document_id != e.child_document_id for e in edges)

    def test_consolidation_edge_links_versions(self) -> None:
        edges = LineageResolver(
            [
                facts("old", doc_type=DocType.CONSOLIDATED, consolidation=date(2015, 1, 1)),
                facts("new", doc_type=DocType.CONSOLIDATED, consolidation=date(2021, 1, 1)),
            ]
        ).build_edges()

        consolidations = [e for e in edges if e.relation_type is RelationType.CONSOLIDATES]
        assert len(consolidations) == 1
        assert consolidations[0].child_document_id == "new"


class TestEmptyCorpus:
    def test_no_documents(self) -> None:
        assert LineageResolver([]).resolve() == []
        assert LineageResolver([]).build_edges() == []
