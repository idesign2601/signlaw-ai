"""Province catalogue.

    Province -> Municipality -> Bylaw documents

The frontend renders whatever this returns and contains no province logic of its
own. Adding Alberta means adding a :class:`ProvinceRecord` and its municipality
records here, then ingesting PDFs — no template, route or JavaScript changes.

**Availability is never declared here.** Whether a municipality can actually be
asked about depends on whether in-force bylaw documents are indexed for it, and
that is a fact about the database, not about this file. A hard-coded
"supported" list would drift from reality the moment an ingest failed, and the
drift would be invisible: the UI would invite a question the corpus cannot
answer. :mod:`app.api.v1.municipalities` joins this catalogue against the
document table to decide.

The registry used for *resolution* (:data:`BC_MUNICIPALITIES`) is deliberately
left BC-only. A municipality this system cannot answer about should not resolve
during query routing — it should fall through to "no relevant bylaw" rather than
produce a confident-looking miss.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.municipalities import (
    BC_MUNICIPALITIES,
    MunicipalityClass,
    MunicipalityRecord,
    slugify,
)

__all__ = [
    "AB_MUNICIPALITIES",
    "PROVINCES",
    "ProvinceRecord",
    "find_municipality",
    "find_province",
]


@dataclass(frozen=True, slots=True)
class ProvinceRecord:
    """One province and the municipalities catalogued for it."""

    code: str
    name: str
    municipalities: tuple[MunicipalityRecord, ...]

    @property
    def slug(self) -> str:
        return slugify(self.name)


C = MunicipalityClass


def _ab(
    name: str,
    classification: MunicipalityClass,
    region: str | None = None,
) -> MunicipalityRecord:
    # Slugs are namespaced by province because municipality names repeat across
    # Canada — there is a Victoria in BC and a Victoria in PEI, and a bare
    # "victoria" key would collide the moment a third province is added.
    return MunicipalityRecord(
        name=name,
        slug=f"ab-{slugify(name)}",
        classification=classification,
        region=region,
    )


# Alberta: catalogued so the coverage list can show what is coming, ingested
# later. Calgary first, per the expansion plan.
AB_MUNICIPALITIES: tuple[MunicipalityRecord, ...] = (
    _ab("Calgary", C.CITY, "Calgary Metropolitan Region"),
)


PROVINCES: tuple[ProvinceRecord, ...] = (
    ProvinceRecord(
        code="BC",
        name="British Columbia",
        municipalities=BC_MUNICIPALITIES,
    ),
    ProvinceRecord(
        code="AB",
        name="Alberta",
        municipalities=AB_MUNICIPALITIES,
    ),
)


def find_province(code: str) -> ProvinceRecord | None:
    """Look up a province by its two-letter code, case-insensitively."""
    wanted = code.strip().upper()
    return next((province for province in PROVINCES if province.code == wanted), None)


def find_municipality(slug: str) -> tuple[ProvinceRecord, MunicipalityRecord] | None:
    """Locate a municipality by slug across every catalogued province.

    Returns the province alongside it, because a citation is meaningless
    without knowing which jurisdiction it came from.
    """
    for province in PROVINCES:
        for municipality in province.municipalities:
            if municipality.slug == slug:
                return province, municipality
    return None
