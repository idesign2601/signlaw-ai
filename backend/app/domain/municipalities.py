"""British Columbia municipality master data.

    Province -> Municipality -> Bylaw documents

Resolving a city name is a correctness problem, not a convenience. "Langley"
is both a City and a Township with separate sign bylaws; "North Vancouver" is
likewise a City and a District. Answering a Langley question from the wrong
Langley's bylaw is a wrong answer that looks completely plausible, so ambiguous
names resolve to *nothing* here and are escalated rather than guessed.

The seed list below covers BC's incorporated municipalities. It is version
controlled rather than fetched at runtime so ingestion is deterministic and
works air-gapped. Verify against the province's official list before relying on
completeness: <https://www2.gov.bc.ca/gov/content/governments/local-governments>
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "BC_MUNICIPALITIES",
    "MunicipalityClass",
    "MunicipalityRecord",
    "MunicipalityRegistry",
    "slugify",
]


class MunicipalityClass(StrEnum):
    """Incorporation type. Part of the official name and often the only thing
    distinguishing two municipalities that share one."""

    CITY = "city"
    DISTRICT = "district"
    TOWN = "town"
    VILLAGE = "village"
    TOWNSHIP = "township"
    REGIONAL_DISTRICT = "regional_district"
    RESORT_MUNICIPALITY = "resort_municipality"
    ISLAND_MUNICIPALITY = "island_municipality"


def slugify(value: str) -> str:
    """Normalise a name to a comparison key.

    Strips accents, punctuation and the incorporation prefix, so "City of
    Coquitlam", "COQUITLAM" and "Coquitlam, B.C." all reduce to ``coquitlam``.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_only = "".join(char for char in decomposed if not unicodedata.combining(char))
    lowered = ascii_only.casefold().strip()

    lowered = re.sub(
        r"^(?:the\s+)?(?:city|corporation|district|township|town|village|"
        r"resort\s+municipality|island\s+municipality|municipality|regional\s+district)"
        r"\s+of\s+",
        "",
        lowered,
    )
    lowered = re.sub(r",?\s*\b(?:b\.?c\.?|british\s+columbia|canada)\b\.?$", "", lowered)
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    return lowered.strip("-")


@dataclass(frozen=True, slots=True)
class MunicipalityRecord:
    """One municipality in the registry."""

    name: str
    slug: str
    classification: MunicipalityClass
    region: str | None = None
    # Alternative spellings seen in filenames and document headers.
    aliases: tuple[str, ...] = ()
    # Set when a bare name is ambiguous. "langley" matches two municipalities,
    # so neither claims the bare slug and the caller must disambiguate.
    ambiguous_bare_name: str | None = None

    @property
    def official_name(self) -> str:
        """Name as it appears on the bylaw, e.g. "City of Coquitlam"."""
        prefix = {
            MunicipalityClass.CITY: "City of",
            MunicipalityClass.DISTRICT: "District of",
            MunicipalityClass.TOWN: "Town of",
            MunicipalityClass.VILLAGE: "Village of",
            MunicipalityClass.TOWNSHIP: "Township of",
            MunicipalityClass.REGIONAL_DISTRICT: "Regional District of",
            MunicipalityClass.RESORT_MUNICIPALITY: "Resort Municipality of",
            MunicipalityClass.ISLAND_MUNICIPALITY: "Island Municipality of",
        }[self.classification]
        return f"{prefix} {self.name}"


def _m(
    name: str,
    classification: MunicipalityClass,
    region: str | None = None,
    *,
    slug: str | None = None,
    aliases: tuple[str, ...] = (),
    ambiguous_bare_name: str | None = None,
) -> MunicipalityRecord:
    return MunicipalityRecord(
        name=name,
        slug=slug or slugify(name),
        classification=classification,
        region=region,
        aliases=aliases,
        ambiguous_bare_name=ambiguous_bare_name,
    )


C = MunicipalityClass

# Metro Vancouver and the Fraser Valley first — these produce the great majority
# of sign-bylaw questions — then the rest of the province.
BC_MUNICIPALITIES: tuple[MunicipalityRecord, ...] = (
    # --- Metro Vancouver ----------------------------------------------------
    _m("Vancouver", C.CITY, "Metro Vancouver", aliases=("Vancouver BC",)),
    _m("Surrey", C.CITY, "Metro Vancouver"),
    _m("Burnaby", C.CITY, "Metro Vancouver"),
    _m("Richmond", C.CITY, "Metro Vancouver"),
    _m("Coquitlam", C.CITY, "Metro Vancouver"),
    _m("Port Coquitlam", C.CITY, "Metro Vancouver", aliases=("PoCo",)),
    _m("Port Moody", C.CITY, "Metro Vancouver"),
    _m("New Westminster", C.CITY, "Metro Vancouver", aliases=("New West",)),
    _m("Delta", C.CITY, "Metro Vancouver", aliases=("Corporation of Delta",)),
    # Langley: City and Township are distinct jurisdictions with distinct
    # bylaws. A bare "Langley" must never silently resolve to either.
    _m(
        "Langley",
        C.CITY,
        "Metro Vancouver",
        slug="langley-city",
        aliases=("City of Langley",),
        ambiguous_bare_name="langley",
    ),
    _m(
        "Langley",
        C.TOWNSHIP,
        "Metro Vancouver",
        slug="langley-township",
        aliases=("Township of Langley",),
        ambiguous_bare_name="langley",
    ),
    # North Vancouver: same problem, City and District.
    _m(
        "North Vancouver",
        C.CITY,
        "Metro Vancouver",
        slug="north-vancouver-city",
        aliases=("City of North Vancouver", "CNV"),
        ambiguous_bare_name="north-vancouver",
    ),
    _m(
        "North Vancouver",
        C.DISTRICT,
        "Metro Vancouver",
        slug="north-vancouver-district",
        aliases=("District of North Vancouver", "DNV"),
        ambiguous_bare_name="north-vancouver",
    ),
    _m("West Vancouver", C.DISTRICT, "Metro Vancouver"),
    _m("Maple Ridge", C.CITY, "Metro Vancouver"),
    _m("Pitt Meadows", C.CITY, "Metro Vancouver"),
    _m("White Rock", C.CITY, "Metro Vancouver"),
    _m("Bowen Island", C.ISLAND_MUNICIPALITY, "Metro Vancouver"),
    _m("Anmore", C.VILLAGE, "Metro Vancouver"),
    _m("Belcarra", C.VILLAGE, "Metro Vancouver"),
    _m("Lions Bay", C.VILLAGE, "Metro Vancouver"),
    # --- Fraser Valley ------------------------------------------------------
    _m("Abbotsford", C.CITY, "Fraser Valley"),
    _m("Chilliwack", C.CITY, "Fraser Valley"),
    _m("Mission", C.DISTRICT, "Fraser Valley"),
    _m("Hope", C.DISTRICT, "Fraser Valley"),
    _m("Kent", C.DISTRICT, "Fraser Valley", aliases=("Agassiz",)),
    _m("Harrison Hot Springs", C.VILLAGE, "Fraser Valley"),
    # --- Capital / Vancouver Island -----------------------------------------
    _m("Victoria", C.CITY, "Capital"),
    _m("Saanich", C.DISTRICT, "Capital"),
    _m("Oak Bay", C.DISTRICT, "Capital"),
    _m("Esquimalt", C.TOWNSHIP, "Capital"),
    _m("Colwood", C.CITY, "Capital"),
    _m("Langford", C.CITY, "Capital"),
    _m("View Royal", C.TOWN, "Capital"),
    _m("Sidney", C.TOWN, "Capital"),
    _m("Central Saanich", C.DISTRICT, "Capital"),
    _m("North Saanich", C.DISTRICT, "Capital"),
    _m("Sooke", C.DISTRICT, "Capital"),
    _m("Metchosin", C.DISTRICT, "Capital"),
    _m("Highlands", C.DISTRICT, "Capital"),
    _m("Nanaimo", C.CITY, "Nanaimo"),
    _m("Lantzville", C.DISTRICT, "Nanaimo"),
    _m("Parksville", C.CITY, "Nanaimo"),
    _m("Qualicum Beach", C.TOWN, "Nanaimo"),
    _m("Ladysmith", C.TOWN, "Cowichan Valley"),
    _m("Duncan", C.CITY, "Cowichan Valley"),
    _m("North Cowichan", C.DISTRICT, "Cowichan Valley"),
    _m("Lake Cowichan", C.TOWN, "Cowichan Valley"),
    _m("Courtenay", C.CITY, "Comox Valley"),
    _m("Comox", C.TOWN, "Comox Valley"),
    _m("Cumberland", C.VILLAGE, "Comox Valley"),
    _m("Campbell River", C.CITY, "Strathcona"),
    _m("Gold River", C.VILLAGE, "Strathcona"),
    _m("Sayward", C.VILLAGE, "Strathcona"),
    _m("Tahsis", C.VILLAGE, "Strathcona"),
    _m("Zeballos", C.VILLAGE, "Strathcona"),
    _m("Port Alberni", C.CITY, "Alberni-Clayoquot"),
    _m("Tofino", C.DISTRICT, "Alberni-Clayoquot"),
    _m("Ucluelet", C.DISTRICT, "Alberni-Clayoquot"),
    _m("Port Hardy", C.DISTRICT, "Mount Waddington"),
    _m("Port McNeill", C.TOWN, "Mount Waddington"),
    _m("Alert Bay", C.VILLAGE, "Mount Waddington"),
    _m("Port Alice", C.VILLAGE, "Mount Waddington"),
    # --- Sunshine Coast / Sea to Sky ----------------------------------------
    _m("Gibsons", C.TOWN, "Sunshine Coast"),
    _m("Sechelt", C.DISTRICT, "Sunshine Coast"),
    _m("Powell River", C.CITY, "qathet"),
    _m("Squamish", C.DISTRICT, "Squamish-Lillooet"),
    _m("Whistler", C.RESORT_MUNICIPALITY, "Squamish-Lillooet"),
    _m("Pemberton", C.VILLAGE, "Squamish-Lillooet"),
    _m("Lillooet", C.DISTRICT, "Squamish-Lillooet"),
    # --- Okanagan / Thompson ------------------------------------------------
    _m("Kelowna", C.CITY, "Central Okanagan"),
    _m("West Kelowna", C.CITY, "Central Okanagan"),
    _m("Lake Country", C.DISTRICT, "Central Okanagan"),
    _m("Peachland", C.DISTRICT, "Central Okanagan"),
    _m("Vernon", C.CITY, "North Okanagan"),
    _m("Coldstream", C.DISTRICT, "North Okanagan"),
    _m("Armstrong", C.CITY, "North Okanagan"),
    _m("Enderby", C.CITY, "North Okanagan"),
    _m("Spallumcheen", C.TOWNSHIP, "North Okanagan"),
    _m("Lumby", C.VILLAGE, "North Okanagan"),
    _m("Penticton", C.CITY, "Okanagan-Similkameen"),
    _m("Summerland", C.DISTRICT, "Okanagan-Similkameen"),
    _m("Oliver", C.TOWN, "Okanagan-Similkameen"),
    _m("Osoyoos", C.TOWN, "Okanagan-Similkameen"),
    _m("Princeton", C.TOWN, "Okanagan-Similkameen"),
    _m("Keremeos", C.VILLAGE, "Okanagan-Similkameen"),
    _m("Kamloops", C.CITY, "Thompson-Nicola"),
    _m("Merritt", C.CITY, "Thompson-Nicola"),
    _m("Logan Lake", C.DISTRICT, "Thompson-Nicola"),
    _m("Barriere", C.DISTRICT, "Thompson-Nicola"),
    _m("Clearwater", C.DISTRICT, "Thompson-Nicola"),
    _m("Chase", C.VILLAGE, "Thompson-Nicola"),
    _m("Ashcroft", C.VILLAGE, "Thompson-Nicola"),
    _m("Cache Creek", C.VILLAGE, "Thompson-Nicola"),
    _m("Clinton", C.VILLAGE, "Thompson-Nicola"),
    _m("Salmon Arm", C.CITY, "Columbia-Shuswap"),
    _m("Sicamous", C.DISTRICT, "Columbia-Shuswap"),
    _m("Revelstoke", C.CITY, "Columbia-Shuswap"),
    _m("Golden", C.TOWN, "Columbia-Shuswap"),
    # --- Kootenays ----------------------------------------------------------
    _m("Cranbrook", C.CITY, "East Kootenay"),
    _m("Kimberley", C.CITY, "East Kootenay"),
    _m("Fernie", C.CITY, "East Kootenay"),
    _m("Invermere", C.DISTRICT, "East Kootenay"),
    _m("Sparwood", C.DISTRICT, "East Kootenay"),
    _m("Elkford", C.DISTRICT, "East Kootenay"),
    _m("Radium Hot Springs", C.VILLAGE, "East Kootenay"),
    _m("Canal Flats", C.VILLAGE, "East Kootenay"),
    _m("Nelson", C.CITY, "Central Kootenay"),
    _m("Castlegar", C.CITY, "Central Kootenay"),
    _m("Creston", C.TOWN, "Central Kootenay"),
    _m("Kaslo", C.VILLAGE, "Central Kootenay"),
    _m("Nakusp", C.VILLAGE, "Central Kootenay"),
    _m("New Denver", C.VILLAGE, "Central Kootenay"),
    _m("Salmo", C.VILLAGE, "Central Kootenay"),
    _m("Silverton", C.VILLAGE, "Central Kootenay"),
    _m("Slocan", C.VILLAGE, "Central Kootenay"),
    _m("Trail", C.CITY, "Kootenay Boundary"),
    _m("Rossland", C.CITY, "Kootenay Boundary"),
    _m("Grand Forks", C.CITY, "Kootenay Boundary"),
    _m("Greenwood", C.CITY, "Kootenay Boundary"),
    _m("Fruitvale", C.VILLAGE, "Kootenay Boundary"),
    _m("Montrose", C.VILLAGE, "Kootenay Boundary"),
    _m("Warfield", C.VILLAGE, "Kootenay Boundary"),
    _m("Midway", C.VILLAGE, "Kootenay Boundary"),
    # --- Cariboo / North ----------------------------------------------------
    _m("Prince George", C.CITY, "Fraser-Fort George"),
    _m("Mackenzie", C.DISTRICT, "Fraser-Fort George"),
    _m("McBride", C.VILLAGE, "Fraser-Fort George"),
    _m("Valemount", C.VILLAGE, "Fraser-Fort George"),
    _m("Williams Lake", C.CITY, "Cariboo"),
    _m("Quesnel", C.CITY, "Cariboo"),
    _m("100 Mile House", C.DISTRICT, "Cariboo", slug="100-mile-house"),
    _m("Wells", C.DISTRICT, "Cariboo"),
    _m("Prince Rupert", C.CITY, "North Coast"),
    _m("Terrace", C.CITY, "Kitimat-Stikine"),
    _m("Kitimat", C.DISTRICT, "Kitimat-Stikine"),
    _m("Smithers", C.TOWN, "Bulkley-Nechako"),
    _m("Houston", C.DISTRICT, "Bulkley-Nechako"),
    _m("Vanderhoof", C.DISTRICT, "Bulkley-Nechako"),
    _m("Fort St. James", C.DISTRICT, "Bulkley-Nechako"),
    _m("Burns Lake", C.VILLAGE, "Bulkley-Nechako"),
    _m("Fraser Lake", C.VILLAGE, "Bulkley-Nechako"),
    _m("Granisle", C.VILLAGE, "Bulkley-Nechako"),
    _m("Telkwa", C.VILLAGE, "Bulkley-Nechako"),
    _m("Fort St. John", C.CITY, "Peace River"),
    _m("Dawson Creek", C.CITY, "Peace River"),
    _m("Chetwynd", C.DISTRICT, "Peace River"),
    _m("Tumbler Ridge", C.DISTRICT, "Peace River"),
    _m("Taylor", C.DISTRICT, "Peace River"),
    _m("Hudson's Hope", C.DISTRICT, "Peace River"),
    _m("Pouce Coupe", C.VILLAGE, "Peace River"),
    _m("Fort Nelson", C.TOWN, "Northern Rockies"),
    _m("Stewart", C.DISTRICT, "Kitimat-Stikine"),
    _m("Masset", C.VILLAGE, "North Coast"),
    _m("Port Clements", C.VILLAGE, "North Coast"),
    _m("Queen Charlotte", C.VILLAGE, "North Coast", aliases=("Daajing Giids",)),
)


@dataclass
class MunicipalityRegistry:
    """Resolves municipality names appearing in filenames and document text.

    Lookup is exact-match on a normalised key. Fuzzy matching is deliberately
    absent: for a legal tool, failing to identify a municipality is recoverable
    through admin review, whereas confidently identifying the wrong one is not.
    """

    records: tuple[MunicipalityRecord, ...] = BC_MUNICIPALITIES
    _by_key: dict[str, MunicipalityRecord] = field(init=False, repr=False)
    _ambiguous: dict[str, tuple[MunicipalityRecord, ...]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._by_key = {}
        ambiguous: dict[str, list[MunicipalityRecord]] = {}

        for record in self.records:
            keys = {record.slug, slugify(record.name), slugify(record.official_name)}
            keys.update(slugify(alias) for alias in record.aliases)

            if record.ambiguous_bare_name:
                # The bare name resolves to nothing; only the qualified forms
                # ("City of Langley") and explicit slugs resolve.
                ambiguous.setdefault(record.ambiguous_bare_name, []).append(record)
                keys.discard(record.ambiguous_bare_name)
                keys.discard(slugify(record.name))

            for key in keys:
                if key:
                    self._by_key.setdefault(key, record)

        for key in ambiguous:
            self._by_key.pop(key, None)

        self._ambiguous = {key: tuple(value) for key, value in ambiguous.items()}

    # -- lookup --------------------------------------------------------------

    def resolve(self, value: str) -> MunicipalityRecord | None:
        """Resolve a name to exactly one municipality, or ``None``.

        ``None`` means either unknown or ambiguous. Use :meth:`candidates` to
        tell the two apart when reporting to an operator.
        """
        if not value or not value.strip():
            return None
        return self._by_key.get(slugify(value))

    def candidates(self, value: str) -> tuple[MunicipalityRecord, ...]:
        """Municipalities a name could refer to.

        Returns two or more entries when the name is ambiguous, one when it
        resolves cleanly, and none when it is unknown.
        """
        key = slugify(value)
        if key in self._ambiguous:
            return self._ambiguous[key]
        record = self._by_key.get(key)
        return (record,) if record else ()

    def is_ambiguous(self, value: str) -> bool:
        return slugify(value) in self._ambiguous

    def find_in_text(self, text: str) -> tuple[MunicipalityRecord, ...]:
        """Every municipality named in a block of text.

        Used against a document's first pages. Longest names are matched first
        so "North Vancouver" is not mistaken for "Vancouver", and "New
        Westminster" is not mistaken for "Westminster".
        """
        haystack = slugify(text)
        found: list[MunicipalityRecord] = []
        seen: set[str] = set()

        for key in sorted(self._by_key, key=len, reverse=True):
            if len(key) < 4:
                continue
            if re.search(rf"(?:^|-){re.escape(key)}(?:-|$)", haystack):
                record = self._by_key[key]
                if record.slug not in seen:
                    seen.add(record.slug)
                    found.append(record)

        return tuple(found)

    def get(self, slug: str) -> MunicipalityRecord | None:
        return next((record for record in self.records if record.slug == slug), None)

    def __len__(self) -> int:
        return len(self.records)
