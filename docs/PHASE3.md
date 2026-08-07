# Phase 3 — zoning, compliance, permits, comparison

Built on one constraint: **no municipal regulation is written in this codebase.**

Every number in every answer is read from indexed bylaw text at question time and
returned with the passage it came from. Where a rule cannot be retrieved, the
answer says so rather than falling back to a default.

---

## Why not a rules table

The obvious design is a table of limits per municipality. It is faster to build,
gives tidy deterministic output, and is wrong for this product.

A coded rule is a second source of truth beside the PDF. It drifts the first time
a municipality amends its bylaw, and the drift is **invisible** — the number
still looks authoritative, still renders in the same box, still carries a
municipality name. The output is the one someone fabricates a sign from.

Retrieval-backed rules fail the other way. When a bylaw changes and a rule
location goes stale, the answer becomes "could not find the governing section" —
visibly wrong, and fixable.

The cost is real: retrieval is slower, parsing prose is harder than reading a
column, and more questions end in "insufficient information". That is the trade
this product is for.

---

## A. Address lookup

`app/services/address.py`

    "123 Main Street Vancouver" -> municipality + street address

Not a geocoder. It splits an address and identifies the municipality; whether the
address exists is answered authoritatively one step later by the city's own
parcel data.

Three refusals worth knowing about:

- **Ambiguous municipality.** "123 Main Street, Langley" returns both candidates.
  The City and the Township have separate zoning *and* separate sign bylaws.
- **No civic number.** "Main Street, Burnaby" is a street, not an address.
  Sending it to a provider would attach sign rules to whichever parcel came back
  first.
- **Street named after a city.** "123 Burnaby Street, Vancouver" is in Vancouver,
  and the street name survives into the lookup.

---

## B. Zoning integration

    app/services/zoning/
        base.py             protocol, result type, address normalisation
        zoning_service.py   caching, staleness, four honest outcomes
        presets.py          suggested configurations, none verified
        providers/
            arcgis.py       ArcGIS REST
            opendatasoft.py Opendatasoft portals
            socrata.py      Socrata

**Three kinds, not N cities.** A kind is a query grammar and is code. A city is a
vocabulary and is data: `gis_provider` names the kind, `gis_config` carries the
field mapping, `gis_endpoint` the URL. Adding Edmonton is one row.

### The verification gate

`gis_verified` defaults to false and **the provider is never built until it is
true.** This is the most important control in the subsystem.

An ArcGIS layer carrying a *different* field for the zone returns nothing,
harmlessly. One carrying a *similar* field returns a confidently wrong zone, and
the service responds happily either way. Searching for Vancouver's zoning API
surfaces an ArcGIS hub with a "parcel zoning" dataset belonging to Vancouver,
**Washington** — plausible, well-formed, wrong country.

So no preset ships verified, and unconfirmed presets ship with no endpoint at all.

### Four outcomes

| Outcome | Meaning |
|---|---|
| `resolved` | Found, current |
| `stale` | Cached, provider unreachable — shown with its as-of date |
| `unsupported` | The city publishes no queryable data, or none is configured |
| `not_found` | The provider ran and matched nothing |

Never a fifth where a plausible zone is invented. Zoning changes by rezoning
application, so cached rows carry `expires_at` and every answer states its date.

---

## C. Compliance engine

    app/services/compliance/
        base.py       SignSpec, RuleLocation, ComplianceReport
        rules.py      where rules live — never what they say
        parsing.py    reading numbers out of prose
        engine.py     retrieve, parse, compare, cite

A `RuleLocation` holds a sign type, a dimension, search terms and expected units.
No limits, no formulas, no thresholds.

Per dimension: retrieve the governing section → parse the limit from the
retrieved text → compare → return the verdict **with the passage it was computed
from**.

### What the parser refuses

More important than what it reads:

| Text | Why nothing is returned |
|---|---|
| "as set out in Schedule B" | Names a limit without stating it |
| "as specified in Section 4.2" | The nearby number is a section, not an area |
| "between 2.4 and 4.5 metres" | A range has no single maximum |
| "9.3 square metres" for a height check | Wrong unit — this is the area sentence |

### Other deliberate choices

- **OCR'd text is excluded entirely.** Everywhere else OCR provenance is
  surfaced and left to the reader. Here the number is measured against and
  fabricated from, and a misread digit is indistinguishable from a correct one.
- **A setback is a minimum**, everything else a maximum. Reversing it reports a
  compliant sign as too close.
- **One indeterminate check makes the whole report indeterminate.** A sign is
  not compliant because the checks that could be evaluated happened to pass.
- **Ratio rules need frontage.** "0.2 m² per metre of frontage" is not a limit
  until a frontage is supplied, and the report says so.

---

## D. Permit checklist

`app/services/permits.py`

Assembled from the bylaw's own permit provisions, each item cited. Topics are
configuration — application, drawings, electrical, engineering, fees, exemptions
— because those are worth asking of any sign bylaw, not because any answer is
assumed.

Topics the bylaw does not address are returned marked `found: false` rather than
omitted: an absent requirement and an unchecked one look identical in a list.

A chunk that never uses the topic's vocabulary is not evidence about it and is
not quoted, however highly retrieval ranked it.

---

## E. Municipality comparison

`app/services/comparison.py`

Each municipality is retrieved and measured **separately**, then placed side by
side. Not one retrieval across both corpora: a single ranked list mixes them, and
an answer attributing Vancouver's limit to Burnaby is wrong in a way that reads
perfectly.

Rows are marked `comparable: false` where the figures are in different units or
different forms. A flat limit beside a per-metre ratio is not a comparison, and
presenting "9.3" next to "0.2" would be actively misleading.

The comparison itself is arithmetic on two measured values, never a judgement.

---

## API

| Endpoint | Auth |
|---|---|
| `POST /api/v1/zoning/lookup` | `X-API-Key` |
| `POST /api/v1/compliance/check` | `X-API-Key` |
| `GET /api/v1/permits/checklist` | `X-API-Key` |
| `POST /api/v1/compare` | `X-API-Key` |
| `GET/PUT /api/v1/admin/municipalities/{slug}/zoning` | `X-Admin-Key` |

All return HTTP 200 for "could not establish". Callers branch on the outcome
field, never on the status code.

---

## Adding a municipality

1. Ingest its bylaw PDFs — the municipality row is created by ingestion.
2. Admin → Zoning providers → pick the city, set kind, endpoint and field mapping.
3. Verify the endpoint against the city's own service directory, then tick
   verified.

No Python changes. Adding a *province* is a `ProvinceRecord` in
`app/domain/provinces.py`; ingestion creates the province row on first document.
