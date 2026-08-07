"""Query understanding.

Runs before retrieval and decides three things: what kind of question this is,
which municipalities it concerns, and whether it is answerable from a sign-bylaw
corpus at all.

The routing matters because these are not one query shape:

* *"Can I install a fascia sign in Coquitlam?"* — one city, one retrieval.
* *"Compare Surrey and Richmond temporary sign rules."* — N city-scoped
  retrievals fanned out in parallel. A single search across both returns
  whichever city's wording better matches the phrasing, producing a lopsided
  comparison that reads as though one city has fewer rules.
* *"What is a fascia sign?"* — a definitions lookup, best served by the
  definition chunks rather than the regulatory clauses.
* *"What's the weather in Vancouver?"* — out of scope. Answering it costs
  nothing but embarrassment; *retrieving* for it wastes a model call and risks
  the synthesiser inventing a plausible bylaw.

Deterministic and pure. No LLM call, so routing adds no latency and cannot
itself hallucinate a municipality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.db.enums import QueryIntent
from app.domain.municipalities import MunicipalityRecord, MunicipalityRegistry

__all__ = ["QueryPlan", "QueryRouter"]


_COMPARE_MARKERS = re.compile(
    r"\b(compare|comparison|versus|vs\.?|difference|differences|differ|"
    r"between|contrast|side by side)\b",
    re.IGNORECASE,
)

_DEFINITION_MARKERS = re.compile(
    r"\b(what (?:is|are|does|do)|define|definition of|meaning of|"
    r"what counts as|considered a)\b",
    re.IGNORECASE,
)

# Vocabulary that indicates a genuine sign-bylaw question. Plurals are matched
# explicitly: `\bpermit\b` does not match "permits", which silently pushed
# "Does Burnaby require permits...?" out of scope.
_DOMAIN_TERMS = re.compile(
    r"\b(signs?|signage|fascia|awnings?|canopy|canopies|banners?|billboards?|"
    r"marquees?|projecting|freestanding|pylons?|monuments?|sandwich boards?|"
    r"a-?frames?|portable|window graphics?|wall murals?|illuminat\w*|"
    r"digital displays?|bylaws?|by-?laws?|permits?|permitted|zoning|zones?|"
    r"setbacks?|frontage|facades?|façades?|heights?|areas?|dimensions?|"
    r"temporary|real estate|construction)\b",
    re.IGNORECASE,
)

# Subjects a sign-bylaw corpus cannot answer, however confidently phrased.
_OUT_OF_SCOPE_TERMS = re.compile(
    r"\b(weather|forecast|restaurant|recipe|stock price|flight|hotel|"
    r"population|election|sports|movie|translate|write (?:me )?a poem|"
    r"tell me a joke)\b",
    re.IGNORECASE,
)

_SIGN_TYPES = (
    "fascia",
    "awning",
    "canopy",
    "projecting",
    "freestanding",
    "pylon",
    "monument",
    "billboard",
    "banner",
    "sandwich board",
    "a-frame",
    "portable",
    "window",
    "roof",
    "marquee",
    "directional",
    "temporary",
    "real estate",
    "construction",
    "digital",
    "electronic",
    "illuminated",
)

# Zoning districts as written in BC bylaws: C-2, RS-1, CD-1, M1, RM-3.
_ZONE_PATTERN = re.compile(r"\b([A-Z]{1,3}-?\d{1,2}[A-Z]?)\b")


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """What retrieval should do with this question."""

    query: str
    intent: QueryIntent
    municipalities: tuple[MunicipalityRecord, ...] = ()
    # Named but unresolvable — "Langley" without City or Township. The caller
    # must ask rather than pick, because answering from the wrong Langley's
    # bylaw is a wrong answer that looks entirely plausible.
    ambiguous_names: tuple[str, ...] = ()
    sign_types: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()
    reason: str = ""

    @property
    def municipality_slugs(self) -> tuple[str, ...]:
        return tuple(record.slug for record in self.municipalities)

    @property
    def is_comparison(self) -> bool:
        return self.intent is QueryIntent.MULTI_CITY_COMPARE

    @property
    def should_retrieve(self) -> bool:
        return self.intent is not QueryIntent.OUT_OF_SCOPE

    @property
    def needs_clarification(self) -> bool:
        """Whether the question cannot be safely answered as asked."""
        return bool(self.ambiguous_names)

    def clarification_prompt(self) -> str | None:
        """Question to put back to the user, when one is needed."""
        if not self.ambiguous_names:
            return None
        name = self.ambiguous_names[0]
        return (
            f"'{name}' matches more than one municipality, each with its own sign "
            f"bylaw. Which did you mean?"
        )


@dataclass
class QueryRouter:
    """Classifies a question and resolves the municipalities it names."""

    registry: MunicipalityRegistry = field(default_factory=MunicipalityRegistry)

    def route(self, query: str) -> QueryPlan:
        """Build a retrieval plan for a question."""
        cleaned = query.strip()
        if not cleaned:
            return QueryPlan(
                query=query,
                intent=QueryIntent.OUT_OF_SCOPE,
                reason="empty question",
            )

        municipalities = self.registry.find_in_text(cleaned)
        ambiguous = self._ambiguous_names(cleaned)

        if self._is_out_of_scope(cleaned, municipalities):
            return QueryPlan(
                query=cleaned,
                intent=QueryIntent.OUT_OF_SCOPE,
                municipalities=municipalities,
                reason="no sign-bylaw vocabulary in the question",
            )

        intent = self._classify(cleaned, municipalities)

        return QueryPlan(
            query=cleaned,
            intent=intent,
            municipalities=municipalities,
            ambiguous_names=ambiguous,
            sign_types=self._sign_types(cleaned),
            zones=self._zones(cleaned),
            reason=self._reason_for(intent, municipalities, ambiguous),
        )

    # -- classification ------------------------------------------------------

    @staticmethod
    def _is_out_of_scope(query: str, municipalities: tuple[MunicipalityRecord, ...]) -> bool:
        """Whether this is answerable from sign bylaws at all.

        A question is in scope if it uses sign-bylaw vocabulary. Naming a BC
        municipality is not enough on its own — "what's the population of
        Surrey?" names one and is still unanswerable here.
        """
        if _OUT_OF_SCOPE_TERMS.search(query):
            return True
        # No domain vocabulary at all means out of scope. A bare city name is
        # not a question this corpus can answer.
        return not _DOMAIN_TERMS.search(query)

    @staticmethod
    def _classify(query: str, municipalities: tuple[MunicipalityRecord, ...]) -> QueryIntent:
        # Two or more cities plus a comparison marker is unambiguous. Two cities
        # without one usually still wants a comparison — "Surrey and Richmond
        # temporary sign rules" — so the city count leads.
        if len(municipalities) >= 2:
            return QueryIntent.MULTI_CITY_COMPARE

        if _COMPARE_MARKERS.search(query) and municipalities:
            return QueryIntent.MULTI_CITY_COMPARE

        # Scope beats phrasing. "What is the maximum sign area in Vancouver?"
        # opens with a definition marker but is a scoped factual question, and
        # routing it as a definition would search the wrong chunks. A definition
        # question that names a city is still a question about that city.
        if municipalities:
            return QueryIntent.SINGLE_CITY

        if _DEFINITION_MARKERS.search(query):
            return QueryIntent.DEFINITION

        # Domain vocabulary but no city: a keyword sweep across the corpus.
        return QueryIntent.KEYWORD

    def _ambiguous_names(self, query: str) -> tuple[str, ...]:
        """Municipality names in the question that resolve to more than one place."""
        found: list[str] = []
        lowered = query.casefold()

        for name in ("langley", "north vancouver"):
            if name in lowered and self.registry.is_ambiguous(name):
                # Qualified forms resolve cleanly, so only flag the bare name.
                qualified = any(
                    prefix in lowered for prefix in ("city of", "township of", "district of")
                )
                if not qualified:
                    found.append(name.title())

        return tuple(found)

    @staticmethod
    def _sign_types(query: str) -> tuple[str, ...]:
        lowered = query.casefold()
        return tuple(term for term in _SIGN_TYPES if term in lowered)

    @staticmethod
    def _zones(query: str) -> tuple[str, ...]:
        """Zoning districts named in the question.

        Most numeric limits are conditional on zone, so a question that names
        one can be answered precisely and a question that does not usually
        cannot — the answer is a table, not a number.
        """
        candidates = _ZONE_PATTERN.findall(query)
        # Exclude bylaw numbers and years caught by the same shape.
        return tuple(
            zone
            for zone in candidates
            if not zone.isdigit() and not re.fullmatch(r"[A-Z]{1,3}", zone)
        )

    @staticmethod
    def _reason_for(
        intent: QueryIntent,
        municipalities: tuple[MunicipalityRecord, ...],
        ambiguous: tuple[str, ...],
    ) -> str:
        if ambiguous:
            return f"ambiguous municipality: {', '.join(ambiguous)}"
        if intent is QueryIntent.MULTI_CITY_COMPARE:
            names = ", ".join(record.name for record in municipalities)
            return f"comparison across {names or 'multiple municipalities'}"
        if intent is QueryIntent.SINGLE_CITY:
            return f"scoped to {municipalities[0].name}"
        if intent is QueryIntent.DEFINITION:
            return "definition lookup"
        return "keyword search across all municipalities"
