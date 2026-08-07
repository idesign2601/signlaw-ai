"""Permit checklist generation.

Same constraint as the compliance engine: **no municipality's requirements are
written here.** A curated checklist per city would be wrong the first time a
municipality revised its application process, and it would be wrong invisibly —
a contractor arrives at the counter missing a document the list never mentioned.

So the checklist is assembled from the bylaw's own permit provisions. Each item
carries the section it came from. Where the bylaw is silent, the item says so
rather than being filled in from what other municipalities usually require.

What *is* configuration here is a set of topics to look for — "application",
"drawings", "electrical", "engineering" — because those are the questions worth
asking of any sign bylaw, not because any particular answer is assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger
from app.rag.results import RetrievedChunk
from app.rag.retriever import RetrievalFilters
from app.services.compliance.base import SignType
from app.services.rag_service import RetrieverProtocol

__all__ = ["ChecklistItem", "ChecklistTopic", "PermitChecklist", "PermitChecklistService"]

logger = get_logger(__name__)


class ChecklistTopic(StrEnum):
    """Questions worth asking of any sign bylaw's permit provisions."""

    APPLICATION = "application"
    DRAWINGS = "drawings"
    SITE_PLAN = "site_plan"
    ELECTRICAL = "electrical"
    STRUCTURAL = "structural"
    FEES = "fees"
    EXEMPTIONS = "exemptions"
    HERITAGE = "heritage"

    @property
    def label(self) -> str:
        return {
            ChecklistTopic.APPLICATION: "Application form and submission",
            ChecklistTopic.DRAWINGS: "Drawings and dimensions",
            ChecklistTopic.SITE_PLAN: "Site plan and location",
            ChecklistTopic.ELECTRICAL: "Electrical permit and connection",
            ChecklistTopic.STRUCTURAL: "Structural and engineering",
            ChecklistTopic.FEES: "Fees",
            ChecklistTopic.EXEMPTIONS: "Whether a permit is required at all",
            ChecklistTopic.HERITAGE: "Heritage and design review",
        }[self]


# Search terms per topic. Vocabulary, not requirements.
_SEARCH_TERMS: dict[ChecklistTopic, tuple[str, ...]] = {
    ChecklistTopic.APPLICATION: ("sign permit application", "application shall include"),
    ChecklistTopic.DRAWINGS: ("drawings", "scaled drawing", "elevation", "dimensions"),
    ChecklistTopic.SITE_PLAN: ("site plan", "location of the sign", "plot plan"),
    ChecklistTopic.ELECTRICAL: ("electrical permit", "electrical connection", "wiring"),
    ChecklistTopic.STRUCTURAL: (
        "structural",
        "professional engineer",
        "sealed drawings",
        "wind load",
    ),
    ChecklistTopic.FEES: ("permit fee", "fees payable", "fee schedule"),
    ChecklistTopic.EXEMPTIONS: ("exempt", "no permit required", "permit is not required"),
    ChecklistTopic.HERITAGE: ("heritage", "design review", "development permit area"),
}


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One requirement, with the text it was drawn from."""

    topic: ChecklistTopic
    label: str
    #: ``False`` when the bylaw says nothing on this topic. Rendered as "not
    #: addressed" rather than omitted, because an absent requirement and an
    #: unchecked one look identical in a list.
    found: bool
    quote: str = ""
    section: str | None = None
    page: int | None = None
    document_title: str | None = None
    bylaw_number: str | None = None
    detail: str = ""


@dataclass
class PermitChecklist:
    """Everything the indexed bylaw says about permitting this sign."""

    municipality_slug: str
    municipality_name: str | None
    sign_type: SignType
    items: list[ChecklistItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def found_items(self) -> list[ChecklistItem]:
        return [item for item in self.items if item.found]

    @property
    def unaddressed(self) -> list[ChecklistItem]:
        return [item for item in self.items if not item.found]

    @property
    def is_useful(self) -> bool:
        """Whether enough was found to be worth showing as a checklist."""
        return len(self.found_items) >= 2


@dataclass
class PermitChecklistService:
    """Assembles a permit checklist from retrieved bylaw text."""

    retriever: RetrieverProtocol
    top_n: int = 3

    async def build(self, sign_type: SignType, municipality_slug: str) -> PermitChecklist:
        checklist = PermitChecklist(
            municipality_slug=municipality_slug,
            municipality_name=None,
            sign_type=sign_type,
        )

        for topic in ChecklistTopic:
            item = await self._item(topic, sign_type, municipality_slug)
            checklist.items.append(item)
            if item.found and checklist.municipality_name is None:
                checklist.municipality_name = item.detail or None

        if not checklist.is_useful:
            checklist.warnings.append(
                "The indexed bylaw says little about permitting. This is not a "
                "complete checklist — contact the municipality's permit desk."
            )

        checklist.warnings.append(
            "Assembled from the bylaw text only. Municipalities also publish "
            "application guides and fee schedules that are not indexed here."
        )

        return checklist

    async def _item(
        self, topic: ChecklistTopic, sign_type: SignType, municipality_slug: str
    ) -> ChecklistItem:
        query = " ".join((*_SEARCH_TERMS[topic][:2], sign_type.value.replace("_", " "), "sign"))

        try:
            chunks, _ = await self.retriever.retrieve(
                query,
                filters=RetrievalFilters(
                    municipality_slugs=(municipality_slug,), in_force_only=True
                ),
                top_n=self.top_n,
            )
        except Exception as exc:
            logger.warning("checklist_retrieval_failed", topic=topic.value, error=str(exc))
            chunks = []

        best = self._best(chunks, topic)
        if best is None:
            return ChecklistItem(
                topic=topic,
                label=topic.label,
                found=False,
                detail="The indexed bylaw does not address this.",
            )

        return ChecklistItem(
            topic=topic,
            label=topic.label,
            found=True,
            quote=_first_sentences(best.body),
            section=best.section_number,
            page=best.page_number,
            document_title=best.document_title,
            bylaw_number=best.bylaw_number,
            detail=best.municipality_name or "",
        )

    @staticmethod
    def _best(chunks: list[RetrievedChunk], topic: ChecklistTopic) -> RetrievedChunk | None:
        """The first chunk that actually mentions the topic.

        Retrieval returns the closest matches whether or not any is close. A
        chunk that never uses the topic's vocabulary is not evidence about it,
        and quoting it would put words in the bylaw's mouth.
        """
        terms = [term.lower() for term in _SEARCH_TERMS[topic]]
        for chunk in chunks:
            body = chunk.body.lower()
            if any(term in body for term in terms):
                return chunk
        return None


def _first_sentences(body: str, limit: int = 320) -> str:
    """A short verbatim extract. Never paraphrased."""
    text = " ".join(body.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(". ", 1)[0]
    return f"{cut}." if cut else f"{text[:limit]}…"
