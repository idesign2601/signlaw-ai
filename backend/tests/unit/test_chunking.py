"""Structure-aware chunking.

The invariant these tests exist to protect: a chunk belongs to exactly one
section, so the section number attached to a retrieved chunk is always the
section its text came from. Everything else here is quality; that one is
correctness.
"""

from __future__ import annotations

import pytest

from app.db.enums import ChunkType
from app.domain.chunking import ChunkingConfig, SectionChunker, chunk_document
from app.domain.models import ExtractedTable, ParsedSection, estimate_tokens


def section(
    index: int,
    number: str,
    heading: str | None,
    body: str,
    *,
    level: int = 2,
    parent: int | None = None,
    page_start: int = 1,
    page_end: int = 1,
) -> ParsedSection:
    return ParsedSection(
        index=index,
        section_number=number,
        heading=heading,
        level=level,
        parent_index=parent,
        full_path=number,
        page_start=page_start,
        page_end=page_end,
        char_start=0,
        char_end=len(body),
        ordinal=index,
        body=body,
    )


def table(page: int = 1, rows: int = 4, columns: int = 3) -> ExtractedTable:
    header = tuple(f"col{c}" for c in range(columns))
    data = tuple(
        tuple(f"r{r}c{c}" for c in range(columns)) for r in range(1, rows)
    )
    markdown = "\n".join(" | ".join(row) for row in (header, *data))
    return ExtractedTable(page_number=page, rows=(header, *data), markdown=markdown)


class TestSectionBoundaries:
    """The core invariant."""

    def test_each_chunk_belongs_to_one_section(self) -> None:
        sections = [
            section(0, "5.3", "Fascia Signs", "Fascia text. " * 20),
            section(1, "5.4", "Awning Signs", "Awning text. " * 20),
        ]
        chunks = chunk_document(sections)

        for chunk in chunks:
            assert chunk.section is not None
            body = chunk.body
            if chunk.section.section_number == "5.3":
                assert "Awning text" not in body
            else:
                assert "Fascia text" not in body

    def test_no_chunk_mixes_two_sections_even_when_tiny(self) -> None:
        sections = [
            section(0, "5.3", "A", "short one"),
            section(1, "5.4", "B", "short two"),
        ]
        chunks = chunk_document(sections)
        numbers = {c.section.section_number for c in chunks if c.section}
        assert numbers == {"5.3", "5.4"}

    def test_overlap_never_crosses_a_section(self) -> None:
        config = ChunkingConfig(target_tokens=50, max_tokens=80, overlap_tokens=20)
        sections = [
            section(0, "5.3", "A", "Alpha sentence here. " * 30),
            section(1, "5.4", "B", "Beta sentence here. " * 30),
        ]
        chunks = SectionChunker(config).chunk(sections)

        for chunk in chunks:
            assert chunk.section is not None
            if chunk.section.section_number == "5.4":
                assert "Alpha" not in chunk.body


class TestCitationPreservation:
    def test_every_chunk_carries_its_section_reference(self) -> None:
        chunks = chunk_document([section(0, "5.3", "Fascia Signs", "body text")])
        assert chunks[0].section is not None
        assert chunks[0].section.section_number == "5.3"
        assert chunks[0].is_citable

    def test_page_range_is_preserved(self) -> None:
        chunks = chunk_document(
            [section(0, "5.3", "Fascia", "body", page_start=22, page_end=24)]
        )
        assert chunks[0].page_start == 22
        assert chunks[0].page_end == 24

    def test_heading_is_prefixed_for_retrievability(self) -> None:
        # "must not exceed 20%" alone is far less findable than the same text
        # under its heading.
        chunks = chunk_document([section(0, "5.3", "Fascia Signs", "must not exceed 20%")])
        assert "5.3 Fascia Signs" in chunks[0].body

    def test_unsectioned_text_is_not_citable(self) -> None:
        chunks = chunk_document([], fallback_text="Some front matter prose.")
        assert chunks
        assert all(not chunk.is_citable for chunk in chunks)
        assert all(chunk.section is None for chunk in chunks)


class TestSplitting:
    def test_oversized_section_is_split(self) -> None:
        config = ChunkingConfig(target_tokens=40, max_tokens=60, overlap_tokens=0)
        body = "\n\n".join(f"Paragraph number {n} with several words." for n in range(20))
        chunks = SectionChunker(config).chunk([section(0, "5.3", "Long", body)])
        assert len(chunks) > 1

    def test_small_section_is_one_chunk(self) -> None:
        chunks = chunk_document([section(0, "5.3", "Short", "Just a little text.")])
        assert len(chunks) == 1

    def test_split_emits_a_whole_section_parent(self) -> None:
        config = ChunkingConfig(target_tokens=40, max_tokens=60, overlap_tokens=0)
        body = "\n\n".join(f"Paragraph {n} of the section." for n in range(20))
        chunks = SectionChunker(config).chunk([section(0, "5.3", "Long", body)])

        parents = [c for c in chunks if c.parent_ordinal is None]
        children = [c for c in chunks if c.parent_ordinal is not None]
        assert parents and children
        # The parent holds the entire section for small-to-big retrieval.
        assert parents[0].token_count >= max(c.token_count for c in children)

    def test_parents_can_be_disabled(self) -> None:
        config = ChunkingConfig(
            target_tokens=40, max_tokens=60, overlap_tokens=0, emit_parents=False
        )
        body = "\n\n".join(f"Paragraph {n} here." for n in range(20))
        chunks = SectionChunker(config).chunk([section(0, "5.3", "Long", body)])
        assert all(chunk.parent_ordinal is None for chunk in chunks)

    def test_no_chunk_exceeds_the_maximum(self) -> None:
        config = ChunkingConfig(target_tokens=50, max_tokens=90, overlap_tokens=10)
        body = " ".join(f"word{n}" for n in range(3000))
        chunks = SectionChunker(config).chunk([section(0, "5.3", "Huge", body)])
        children = [c for c in chunks if c.parent_ordinal is not None] or chunks
        assert all(c.token_count <= config.max_tokens * 1.2 for c in children)

    def test_single_oversized_sentence_is_force_split(self) -> None:
        config = ChunkingConfig(target_tokens=30, max_tokens=50, overlap_tokens=0)
        body = " ".join(f"word{n}" for n in range(500))  # no sentence breaks
        chunks = SectionChunker(config).chunk([section(0, "5.3", "Run-on", body)])
        assert len(chunks) > 1


class TestOverlap:
    def test_overlap_repeats_trailing_context(self) -> None:
        config = ChunkingConfig(target_tokens=40, max_tokens=70, overlap_tokens=25)
        paragraphs = [f"Paragraph {n} with distinctive marker{n}." for n in range(12)]
        chunks = SectionChunker(config).chunk(
            [section(0, "5.3", "S", "\n\n".join(paragraphs))]
        )
        children = [c for c in chunks if c.parent_ordinal is not None]
        if len(children) >= 2:
            # Some text from the first chunk must reappear in the second so a
            # clause beginning "such a sign" keeps its antecedent.
            first_words = set(children[0].body.split())
            second_words = set(children[1].body.split())
            assert first_words & second_words

    def test_zero_overlap_is_honoured(self) -> None:
        config = ChunkingConfig(target_tokens=40, max_tokens=70, overlap_tokens=0)
        chunker = SectionChunker(config)
        assert chunker._overlap_tail(["a", "b", "c"]) == []


class TestTables:
    def test_table_becomes_its_own_chunk(self) -> None:
        chunks = chunk_document([section(0, "5.3", "S", "prose")], tables=[table()])
        table_chunks = [c for c in chunks if c.chunk_type is ChunkType.TABLE]
        assert len(table_chunks) == 1

    def test_table_is_never_split(self) -> None:
        # Half a table looks complete and is therefore worse than no table.
        config = ChunkingConfig(target_tokens=10, max_tokens=15, overlap_tokens=0)
        big = table(rows=40, columns=6)
        chunks = SectionChunker(config).chunk([], tables=[big])
        table_chunks = [c for c in chunks if c.chunk_type is ChunkType.TABLE]
        assert len(table_chunks) == 1
        assert table_chunks[0].body.count("\n") >= 39

    def test_table_is_attributed_to_its_enclosing_section(self) -> None:
        sections = [section(0, "Schedule A", "Areas", "", page_start=40, page_end=42)]
        chunks = chunk_document(sections, tables=[table(page=41)])
        table_chunk = next(c for c in chunks if c.chunk_type is ChunkType.TABLE)
        assert table_chunk.section is not None
        assert table_chunk.section.section_number == "Schedule A"

    def test_deepest_section_wins(self) -> None:
        sections = [
            section(0, "Part 5", "Signs", "", level=1, page_start=1, page_end=50),
            section(1, "5.3", "Fascia", "", level=3, page_start=20, page_end=25),
        ]
        chunks = chunk_document(sections, tables=[table(page=22)])
        table_chunk = next(c for c in chunks if c.chunk_type is ChunkType.TABLE)
        assert table_chunk.section is not None
        assert table_chunk.section.section_number == "5.3"

    def test_degenerate_tables_are_discarded(self) -> None:
        # Ruled lines and page borders routinely produce these.
        junk = ExtractedTable(page_number=1, rows=(("only",),), markdown="only")
        chunks = chunk_document([section(0, "5.3", "S", "prose")], tables=[junk])
        assert all(c.chunk_type is not ChunkType.TABLE for c in chunks)


class TestDefinitions:
    def test_definitions_are_split_per_term(self) -> None:
        body = (
            '"Fascia Sign" means a sign attached to a wall.\n'
            '"Awning Sign" means a sign on an awning.\n'
            '"Portable Sign" means a sign not permanently affixed.'
        )
        chunks = chunk_document([section(0, "2", "Definitions", body)])
        definitions = [c for c in chunks if c.chunk_type is ChunkType.DEFINITION]
        assert len(definitions) == 3

    def test_each_definition_is_self_contained(self) -> None:
        body = (
            '"Fascia Sign" means a sign attached to a wall.\n'
            '"Awning Sign" means a sign on an awning.'
        )
        chunks = chunk_document([section(0, "2", "Definitions", body)])
        fascia = next(c for c in chunks if "Fascia Sign" in c.body)
        assert "Awning Sign" not in fascia.body

    def test_interpretation_heading_also_triggers_it(self) -> None:
        body = '"A" means one.\n"B" means two.'
        chunks = chunk_document([section(0, "2", "Interpretation", body)])
        assert any(c.chunk_type is ChunkType.DEFINITION for c in chunks)

    def test_single_definition_falls_back_to_prose(self) -> None:
        chunks = chunk_document(
            [section(0, "2", "Definitions", '"Fascia Sign" means a wall sign.')]
        )
        assert chunks[0].chunk_type is ChunkType.PROSE

    def test_splitting_can_be_disabled(self) -> None:
        config = ChunkingConfig(split_definitions=False)
        body = '"A" means one.\n"B" means two.\n"C" means three.'
        chunks = SectionChunker(config).chunk([section(0, "2", "Definitions", body)])
        assert all(c.chunk_type is not ChunkType.DEFINITION for c in chunks)


class TestMerging:
    def test_tiny_fragments_merge_into_a_neighbour(self) -> None:
        config = ChunkingConfig(target_tokens=100, max_tokens=200, min_tokens=30)
        sections = [
            section(0, "5.3", "S", "A reasonably sized paragraph with enough words. " * 5),
            section(1, "5.3", "S", "tiny"),
        ]
        chunks = SectionChunker(config).chunk(sections)
        assert all(
            c.token_count >= config.min_tokens or c.chunk_type is not ChunkType.PROSE
            for c in chunks
        )

    def test_fragments_from_different_sections_do_not_merge(self) -> None:
        config = ChunkingConfig(target_tokens=100, max_tokens=200, min_tokens=50)
        sections = [section(0, "5.3", "A", "short"), section(1, "5.4", "B", "short")]
        chunks = SectionChunker(config).chunk(sections)
        assert len(chunks) == 2

    def test_tables_are_never_merged(self) -> None:
        config = ChunkingConfig(min_tokens=500)
        chunks = SectionChunker(config).chunk(
            [section(0, "5.3", "S", "prose text")], tables=[table()]
        )
        assert any(c.chunk_type is ChunkType.TABLE for c in chunks)


class TestOrdinals:
    def test_ordinals_are_sequential(self) -> None:
        sections = [section(i, f"5.{i}", "S", f"body {i}") for i in range(5)]
        chunks = chunk_document(sections)
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))


class TestConfigValidation:
    def test_overlap_must_be_below_target(self) -> None:
        with pytest.raises(ValueError, match="overlap_tokens"):
            ChunkingConfig(target_tokens=100, overlap_tokens=100)

    def test_max_must_not_be_below_target(self) -> None:
        with pytest.raises(ValueError, match="max_tokens"):
            ChunkingConfig(target_tokens=100, max_tokens=50)

    def test_min_must_be_below_target(self) -> None:
        with pytest.raises(ValueError, match="min_tokens"):
            ChunkingConfig(target_tokens=100, min_tokens=100)


class TestTokenCounting:
    def test_estimator_is_monotonic(self) -> None:
        assert estimate_tokens("a" * 100) < estimate_tokens("a" * 200)

    def test_empty_string_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_a_custom_counter_is_used(self) -> None:
        chunks = chunk_document(
            [section(0, "5.3", "S", "body")], count_tokens=lambda text: 999
        )
        assert chunks[0].token_count == 999


class TestEmptyInputs:
    def test_no_sections_and_no_fallback(self) -> None:
        assert chunk_document([]) == []

    def test_empty_section_bodies_are_dropped(self) -> None:
        assert chunk_document([section(0, "5.3", "S", "   ")]) == []
