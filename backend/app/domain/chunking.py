"""Structure-aware chunking for legal documents.

Fixed-size chunking is wrong for this corpus. Slicing every 700 tokens cuts
through ``5.3(b)`` mid-clause and produces chunks that straddle two sections,
so the section number attached to a retrieved chunk is not reliably the section
the text came from. For a system whose output is a legal citation, that is a
correctness bug, not a quality tradeoff.

The rules here, in priority order:

1. **Never cross a section boundary.** A chunk belongs to exactly one section,
   so its citation is always right.
2. **Never split a table.** Tables carry the numeric limits most questions turn
   on; half a table is worse than no table, because the surviving rows look
   complete.
3. **Split oversized sections at paragraph, then sentence, boundaries** — and
   emit a whole-section parent alongside the pieces, so retrieval can match a
   precise child and hand the model the full section for context.
4. **Merge undersized fragments** with a neighbour in the same section, rather
   than indexing a chunk too small to mean anything on its own.
5. **Chunk definitions one term at a time.** ``"Fascia Sign" means ...`` is a
   self-contained retrievable unit and is asked about directly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.db.enums import ChunkType
from app.domain.models import (
    ExtractedTable,
    ParsedSection,
    SectionRef,
    TextChunk,
    TokenCounter,
    estimate_tokens,
)

__all__ = ["ChunkingConfig", "SectionChunker", "chunk_document"]


# A definitions section: `"Fascia Sign" means a sign attached to ...`
# `{0,80}` not `{1,80}`: single-letter defined terms exist ("\"A\" means ...")
# and a quantifier requiring a second character silently skipped them.
_DEFINITION_TERM = re.compile(
    r'^\s*[“"]?(?P<term>[A-Z][^"”\n]{0,80})[”"]?\s+'
    r"(?:means|includes|shall mean)\b",
    re.MULTILINE,
)

_DEFINITIONS_HEADING = re.compile(
    r"\b(?:definitions?|interpretation)\b", re.IGNORECASE
)

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")

# Sentence boundary that tolerates the abbreviations common in bylaws
# ("No.", "s.", "Sched.", "Ltd.") without splitting on them.
_SENTENCE_BREAK = re.compile(
    r"(?<![A-Z])(?<!\bNo)(?<!\bs)(?<!\bSched)(?<!\bLtd)(?<!\bCo)"
    r"(?<=[.!?])\s+(?=[A-Z(])"
)


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    """Chunk sizing. Mirrors the ``INGESTION__CHUNK_*`` settings."""

    target_tokens: int = 700
    max_tokens: int = 1200
    overlap_tokens: int = 80
    # Below this a chunk is merged into a neighbour. ``None`` derives it from
    # the target, so a caller that shrinks target_tokens for a test or a small
    # corpus does not also have to remember to shrink this. A fixed default
    # would silently exceed any target below 48 and reject valid configuration.
    min_tokens: int | None = None
    # Emit a whole-section parent chunk when a section is split.
    emit_parents: bool = True
    # Split a definitions section into one chunk per defined term.
    split_definitions: bool = True

    def __post_init__(self) -> None:
        if self.overlap_tokens >= self.target_tokens:
            raise ValueError("overlap_tokens must be smaller than target_tokens")
        if self.max_tokens < self.target_tokens:
            raise ValueError("max_tokens must be >= target_tokens")
        if self.min_tokens is not None and self.min_tokens >= self.target_tokens:
            raise ValueError("min_tokens must be smaller than target_tokens")

    @property
    def effective_min_tokens(self) -> int:
        """Merge threshold, derived when not set explicitly."""
        if self.min_tokens is not None:
            return self.min_tokens
        return min(48, max(1, self.target_tokens // 8))


class SectionChunker:
    """Turns parsed sections and tables into citable chunks."""

    def __init__(
        self,
        config: ChunkingConfig | None = None,
        *,
        count_tokens: TokenCounter = estimate_tokens,
    ) -> None:
        self.config = config or ChunkingConfig()
        self.count_tokens = count_tokens

    # -- public API ----------------------------------------------------------

    def chunk(
        self,
        sections: Sequence[ParsedSection],
        *,
        tables: Sequence[ExtractedTable] = (),
        fallback_text: str = "",
        fallback_page: int = 1,
    ) -> list[TextChunk]:
        """Chunk a document.

        ``fallback_text`` is used when no sections were recognised. Those chunks
        carry no section reference, which is what marks them as uncitable at the
        clause level rather than letting them masquerade as law.
        """
        chunks: list[TextChunk] = []

        if sections:
            for section in sections:
                chunks.extend(self._chunk_section(section))
        elif fallback_text.strip():
            chunks.extend(
                self._split_text(
                    fallback_text,
                    section=None,
                    page_start=fallback_page,
                    page_end=fallback_page,
                )
            )

        chunks.extend(self._table_chunks(tables, sections))
        return self._finalise(chunks)

    # -- section handling ----------------------------------------------------

    def _chunk_section(self, section: ParsedSection) -> list[TextChunk]:
        body = section.body.strip()
        if not body:
            return []

        ref = section.ref

        if self.config.split_definitions and self._is_definitions_section(section):
            definition_chunks = self._split_definitions(body, ref, section)
            if definition_chunks:
                return definition_chunks

        tokens = self.count_tokens(body)
        if tokens <= self.config.max_tokens:
            return [
                TextChunk(
                    body=self._with_heading(section, body),
                    page_start=section.page_start,
                    page_end=section.page_end,
                    token_count=tokens,
                    chunk_type=ChunkType.PROSE,
                    section=ref,
                )
            ]

        return self._split_text(
            body,
            section=ref,
            page_start=section.page_start,
            page_end=section.page_end,
            heading_prefix=self._heading_prefix(section),
        )

    @staticmethod
    def _is_definitions_section(section: ParsedSection) -> bool:
        return bool(section.heading and _DEFINITIONS_HEADING.search(section.heading))

    def _split_definitions(
        self, body: str, ref: SectionRef, section: ParsedSection
    ) -> list[TextChunk]:
        """One chunk per defined term.

        Definitions sections are long lists of independent statements. Chunked
        by size they interleave unrelated terms; chunked by term each unit is
        exactly what a "what counts as a fascia sign?" query should retrieve.
        """
        matches = list(_DEFINITION_TERM.finditer(body))
        if len(matches) < 2:
            return []

        chunks: list[TextChunk] = []
        for position, match in enumerate(matches):
            start = match.start()
            end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
            text = body[start:end].strip()
            if not text:
                continue

            chunks.append(
                TextChunk(
                    body=f"{self._heading_prefix(section)}{text}",
                    page_start=section.page_start,
                    page_end=section.page_end,
                    token_count=self.count_tokens(text),
                    chunk_type=ChunkType.DEFINITION,
                    section=ref,
                )
            )
        return chunks

    # -- splitting -----------------------------------------------------------

    def _split_text(
        self,
        text: str,
        *,
        section: SectionRef | None,
        page_start: int,
        page_end: int,
        heading_prefix: str = "",
    ) -> list[TextChunk]:
        """Split oversized text, preferring paragraph then sentence boundaries.

        Overlap is applied only *within* this call, so it never bleeds across a
        section boundary and never causes a chunk to carry text belonging to a
        different clause.
        """
        units = self._split_units(text)
        chunks: list[TextChunk] = []
        buffer: list[str] = []
        buffer_tokens = 0

        def flush() -> None:
            nonlocal buffer, buffer_tokens
            if not buffer:
                return
            body = " ".join(buffer).strip()
            if body:
                chunks.append(
                    TextChunk(
                        body=f"{heading_prefix}{body}",
                        page_start=page_start,
                        page_end=page_end,
                        token_count=self.count_tokens(body),
                        chunk_type=ChunkType.PROSE,
                        section=section,
                    )
                )
            buffer = []
            buffer_tokens = 0

        for unit in units:
            unit_tokens = self.count_tokens(unit)

            # A single unit larger than the maximum must be split by force,
            # otherwise it would overflow the model's context window.
            if unit_tokens > self.config.max_tokens:
                flush()
                for piece in self._hard_split(unit):
                    chunks.append(
                        TextChunk(
                            body=f"{heading_prefix}{piece}",
                            page_start=page_start,
                            page_end=page_end,
                            token_count=self.count_tokens(piece),
                            chunk_type=ChunkType.PROSE,
                            section=section,
                        )
                    )
                continue

            if buffer_tokens + unit_tokens > self.config.target_tokens and buffer:
                overlap = self._overlap_tail(buffer)
                flush()
                buffer = list(overlap)
                buffer_tokens = sum(self.count_tokens(item) for item in buffer)

            buffer.append(unit)
            buffer_tokens += unit_tokens

        flush()

        if self.config.emit_parents and len(chunks) > 1 and section is not None:
            chunks = self._with_parent(chunks, text, section, page_start, page_end, heading_prefix)

        return chunks

    def _split_units(self, text: str) -> list[str]:
        """Break text into the smallest units the splitter will not divide."""
        units: list[str] = []
        for paragraph in _PARAGRAPH_BREAK.split(text):
            cleaned = paragraph.strip()
            if not cleaned:
                continue
            if self.count_tokens(cleaned) <= self.config.target_tokens:
                units.append(cleaned)
                continue
            units.extend(
                sentence.strip()
                for sentence in _SENTENCE_BREAK.split(cleaned)
                if sentence.strip()
            )
        return units

    def _hard_split(self, text: str) -> list[str]:
        """Last-resort split of a single oversized sentence, on word boundaries."""
        words = text.split()
        if not words:
            return []

        # Derived from the token budget rather than a fixed word count, so this
        # tracks whatever tokenizer is injected.
        words_per_chunk = max(
            1, int(len(words) * self.config.target_tokens / max(1, self.count_tokens(text)))
        )

        return [
            " ".join(words[start : start + words_per_chunk])
            for start in range(0, len(words), words_per_chunk)
        ]

    def _overlap_tail(self, buffer: Sequence[str]) -> list[str]:
        """Trailing units to repeat at the head of the next chunk.

        Overlap preserves the antecedent of a clause that begins with "such a
        sign" or "the foregoing", which would otherwise be unresolvable.
        """
        if self.config.overlap_tokens <= 0:
            return []

        tail: list[str] = []
        total = 0
        for unit in reversed(buffer):
            unit_tokens = self.count_tokens(unit)
            if total + unit_tokens > self.config.overlap_tokens and tail:
                break
            tail.insert(0, unit)
            total += unit_tokens
        return tail

    def _with_parent(
        self,
        children: list[TextChunk],
        full_text: str,
        section: SectionRef,
        page_start: int,
        page_end: int,
        heading_prefix: str,
    ) -> list[TextChunk]:
        """Prepend a whole-section parent and link the children to it.

        Small-to-big retrieval: the children are precise enough to match a
        specific question, the parent is complete enough to answer it.
        """
        parent = TextChunk(
            body=f"{heading_prefix}{full_text.strip()}",
            page_start=page_start,
            page_end=page_end,
            token_count=self.count_tokens(full_text),
            chunk_type=ChunkType.PROSE,
            section=section,
            ordinal=0,
        )
        linked = [
            TextChunk(
                body=child.body,
                page_start=child.page_start,
                page_end=child.page_end,
                token_count=child.token_count,
                chunk_type=child.chunk_type,
                section=child.section,
                ordinal=child.ordinal,
                parent_ordinal=0,
            )
            for child in children
        ]
        return [parent, *linked]

    # -- tables --------------------------------------------------------------

    def _table_chunks(
        self, tables: Sequence[ExtractedTable], sections: Sequence[ParsedSection]
    ) -> list[TextChunk]:
        """One chunk per table, never split, attributed to its enclosing section."""
        chunks: list[TextChunk] = []

        for table in tables:
            if table.is_degenerate:
                continue

            section = self._section_for_page(sections, table.page_number)
            caption = f"{table.caption}\n" if table.caption else ""
            body = f"{caption}{table.markdown}"

            chunks.append(
                TextChunk(
                    body=body,
                    page_start=table.page_number,
                    page_end=table.page_number,
                    token_count=self.count_tokens(body),
                    chunk_type=ChunkType.TABLE,
                    section=section.ref if section else None,
                )
            )
        return chunks

    @staticmethod
    def _section_for_page(
        sections: Sequence[ParsedSection], page_number: int
    ) -> ParsedSection | None:
        """Deepest section spanning the page a table sits on."""
        candidates = [
            section
            for section in sections
            if section.page_start <= page_number <= section.page_end
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda section: section.level)

    # -- finalising ----------------------------------------------------------

    def _finalise(self, chunks: Iterable[TextChunk]) -> list[TextChunk]:
        """Drop empties, merge undersized fragments, assign ordinals."""
        kept = [chunk for chunk in chunks if chunk.body.strip()]
        merged = self._merge_undersized(kept)

        return [
            TextChunk(
                body=chunk.body,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                token_count=chunk.token_count,
                chunk_type=chunk.chunk_type,
                section=chunk.section,
                ordinal=position,
                parent_ordinal=chunk.parent_ordinal,
            )
            for position, chunk in enumerate(merged)
        ]

    def _merge_undersized(self, chunks: list[TextChunk]) -> list[TextChunk]:
        """Fold tiny chunks into the previous chunk of the same section.

        A 12-token fragment embeds to noise and pollutes retrieval. Tables and
        definitions are exempt: a short table row set is still a complete unit,
        and a one-line definition is exactly what should be retrievable.
        """
        if not chunks:
            return []

        result: list[TextChunk] = [chunks[0]]

        for chunk in chunks[1:]:
            previous = result[-1]
            mergeable = (
                chunk.token_count < self.config.effective_min_tokens
                and chunk.chunk_type is ChunkType.PROSE
                and previous.chunk_type is ChunkType.PROSE
                and chunk.section == previous.section
                and previous.token_count + chunk.token_count <= self.config.max_tokens
            )

            if not mergeable:
                result.append(chunk)
                continue

            body = f"{previous.body}\n{chunk.body}".strip()
            result[-1] = TextChunk(
                body=body,
                page_start=previous.page_start,
                page_end=max(previous.page_end, chunk.page_end),
                token_count=self.count_tokens(body),
                chunk_type=previous.chunk_type,
                section=previous.section,
                ordinal=previous.ordinal,
                parent_ordinal=previous.parent_ordinal,
            )

        return result

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _heading_prefix(section: ParsedSection) -> str:
        """Heading line repeated at the top of every chunk from a section.

        Costs a few tokens and materially improves retrieval: an embedded chunk
        reading only "must not exceed 20% of the façade" is far less findable
        than the same text under "5.3 Fascia Signs".
        """
        label = section.section_number
        if section.heading:
            label = f"{label} {section.heading}".strip()
        return f"[{label}]\n" if label else ""

    def _with_heading(self, section: ParsedSection, body: str) -> str:
        return f"{self._heading_prefix(section)}{body}"


def chunk_document(
    sections: Sequence[ParsedSection],
    *,
    tables: Sequence[ExtractedTable] = (),
    config: ChunkingConfig | None = None,
    fallback_text: str = "",
    count_tokens: TokenCounter = estimate_tokens,
) -> list[TextChunk]:
    """Convenience wrapper around :class:`SectionChunker`."""
    chunker = SectionChunker(config, count_tokens=count_tokens)
    return chunker.chunk(sections, tables=tables, fallback_text=fallback_text)
