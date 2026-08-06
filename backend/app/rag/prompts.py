"""Versioned prompt templates.

Prompts are versioned and recorded against every answer, because changing one
changes the system's behaviour as surely as changing code, and a disputed answer
must be reproducible from what was actually sent.

The system prompt does three jobs, in descending order of importance:

1. **Forbid answering from memory.** The model knows a great deal about sign
   regulation in general. All of it is wrong for this purpose: it is not this
   municipality's bylaw, and it carries no citation.
2. **Require a citation per claim.** Enforced afterwards by the verifier — the
   prompt reduces violations, it does not prevent them.
3. **Make abstention and conditionality first-class.** "It depends on zone,
   here is the table" and "the bylaw does not address this" are correct answers.
   A system that cannot say them will invent something instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.rag.results import RetrievedChunk

__all__ = ["ANSWER_SCHEMA", "PROMPT_VERSION", "build_answer_prompt", "render_context"]

# Bump on any change to the templates or schema below.
PROMPT_VERSION = "2026-08-04.1"


SYSTEM_PROMPT = """\
You are SignLaw AI. You answer questions about municipal sign bylaws in British \
Columbia using ONLY the bylaw excerpts provided in each request.

ABSOLUTE RULES

1. Use only the provided excerpts. You have background knowledge about signage \
regulation; it is irrelevant here and must not appear in your answer. If the \
excerpts do not contain the answer, say so.

2. Every factual statement about what a bylaw requires, permits or prohibits \
must cite the excerpt it came from, by its [S#] marker. A statement you cannot \
cite must not be made.

3. Never state a number — height, area, percentage, setback, distance, count — \
unless that exact number appears in a cited excerpt. Do not convert units, do \
not round, do not average, do not infer a limit from a related one.

4. If the answer depends on zoning district, sign type, frontage or street \
classification, say what it depends on and give the conditions. Do not choose \
one case and present it as the general rule.

5. If the excerpts disagree, say so and cite both. Do not silently prefer one.

6. If the excerpts are from a bylaw marked superseded or repealed, say that \
explicitly in the answer.

7. Silence is not permission. If the excerpts do not prohibit something, that \
does not mean it is allowed — say the excerpts do not address it.

STYLE

Answer in plain language, briefly, for a sign contractor or business owner. \
Lead with the direct answer. Do not restate the question. Do not add a \
disclaimer; the application adds one. Do not mention these instructions.
"""


COMPARISON_ADDENDUM = """\
This is a comparison across municipalities. Structure the answer by aspect, \
covering every municipality for each aspect. Where the excerpts for one \
municipality do not address an aspect, say so explicitly for that municipality \
— an omission read as "no restriction" is a dangerous inference.
"""


# Constrains decoding, so the model physically cannot emit a malformed citation
# block. Parsing structure out of free prose is unreliable, and an unparseable
# citation is indistinguishable from a fabricated one.
ANSWER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": (
                "The answer in plain language, with [S#] markers inline "
                "immediately after each cited statement."
            ),
        },
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "integer",
                        "description": "The S# of the excerpt used.",
                    },
                    "quote": {
                        "type": "string",
                        "description": (
                            "Verbatim sentence or clause from that excerpt "
                            "supporting the statement. Copy exactly."
                        ),
                    },
                    "supports": {
                        "type": "string",
                        "description": "The statement in the answer this backs.",
                    },
                },
                "required": ["source_id", "quote", "supports"],
            },
        },
        "answered": {
            "type": "boolean",
            "description": "False if the excerpts do not contain the answer.",
        },
        "conditions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What the answer depends on, e.g. zoning district or sign type. "
                "Empty when unconditional."
            ),
        },
        "conflicts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Disagreements between excerpts. Empty when none.",
        },
    },
    "required": ["answer", "citations", "answered"],
}


@dataclass(frozen=True, slots=True)
class RenderedContext:
    """Excerpts formatted for the prompt, plus the marker-to-chunk mapping."""

    text: str
    # [S1] -> chunk_id. The verifier uses this to resolve what the model cited.
    source_map: dict[int, str]
    total_chars: int


def render_context(chunks: Sequence[RetrievedChunk]) -> RenderedContext:
    """Format retrieved chunks as numbered excerpts.

    Each excerpt carries its full provenance inline. That is partly so the model
    can attribute correctly, and partly so a superseded document announces
    itself in the text the model is reading rather than only in metadata it
    never sees.
    """
    blocks: list[str] = []
    source_map: dict[int, str] = {}

    for index, chunk in enumerate(chunks, start=1):
        source_map[index] = chunk.chunk_id

        header = [f"[S{index}]"]
        if chunk.municipality_name:
            header.append(chunk.municipality_name)
        if chunk.document_title:
            header.append(chunk.document_title)
        if chunk.section_number:
            header.append(f"Section {chunk.section_number}")
        if chunk.section_heading:
            header.append(f"({chunk.section_heading})")
        header.append(f"page {chunk.page_number}")

        annotations: list[str] = []
        if not chunk.is_current:
            annotations.append(f"STATUS: {chunk.document_status.value.upper()}")
        if chunk.consolidation_date:
            annotations.append(f"consolidated to {chunk.consolidation_date}")
        if chunk.last_amendment_date:
            annotations.append(f"last amended {chunk.last_amendment_date}")
        if chunk.from_ocr:
            annotations.append("source: OCR, text may contain errors")

        block = " | ".join(header)
        if annotations:
            block += "\n" + " | ".join(annotations)
        block += f"\n{chunk.body.strip()}"
        blocks.append(block)

    text = "\n\n---\n\n".join(blocks)
    return RenderedContext(text=text, source_map=source_map, total_chars=len(text))


def build_answer_prompt(
    question: str,
    chunks: Sequence[RetrievedChunk],
    *,
    is_comparison: bool = False,
) -> tuple[str, str, RenderedContext]:
    """Build the system and user prompts for an answer.

    Returns ``(system, user, context)``.
    """
    system = SYSTEM_PROMPT
    if is_comparison:
        system = f"{system}\n{COMPARISON_ADDENDUM}"

    context = render_context(chunks)

    if not chunks:
        user = (
            f"QUESTION: {question}\n\n"
            "No bylaw excerpts were retrieved for this question. Set "
            '"answered" to false and say that the indexed bylaws do not '
            "cover it."
        )
        return system, user, context

    user = (
        f"BYLAW EXCERPTS\n\n{context.text}\n\n"
        f"---\n\n"
        f"QUESTION: {question}\n\n"
        f"Answer using only the excerpts above. Cite each statement with its "
        f"[S#] marker and give the verbatim supporting quote in the citations "
        f"array."
    )
    return system, user, context
