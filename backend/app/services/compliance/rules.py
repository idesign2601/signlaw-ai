"""Rule locations — where to look, never what the rule says.

Every entry below is a search strategy. None of them contains a limit, a
threshold or a formula, and none ever should: the numbers live in the bylaw PDFs
and are read from them at question time.

Adding a municipality does not require touching this file. These locations are
generic across BC and Alberta sign bylaws because they describe *vocabulary*,
not regulation. A municipality with unusual terminology gets an override in
:data:`MUNICIPALITY_OVERRIDES`, which is still only search terms.
"""

from __future__ import annotations

from app.services.compliance.base import Dimension, RuleLocation, SignType

__all__ = ["MUNICIPALITY_OVERRIDES", "RULE_LOCATIONS", "locations_for"]


def _location(
    sign_type: SignType,
    dimension: Dimension,
    terms: tuple[str, ...],
    units: tuple[str, ...] = (),
    *,
    ratio: bool = False,
    notes: str = "",
) -> RuleLocation:
    return RuleLocation(
        sign_type=sign_type,
        dimension=dimension,
        search_terms=terms,
        expected_units=units,
        is_ratio_of_frontage=ratio,
        notes=notes,
    )


_AREA_UNITS = ("m2", "ft2")
_LENGTH_UNITS = ("m", "ft")

# Fascia and channel letters share regulation in most bylaws: channel letters
# are a construction method for a fascia sign, not a separate class. Where a
# municipality does treat them separately, an override picks up the difference.
_FASCIA_TERMS = ("fascia sign", "wall sign", "maximum sign area")

RULE_LOCATIONS: tuple[RuleLocation, ...] = (
    # --- fascia --------------------------------------------------------------
    _location(
        SignType.FASCIA,
        Dimension.AREA,
        (*_FASCIA_TERMS, "sign area per metre of frontage"),
        _AREA_UNITS,
        ratio=True,
        notes="Usually expressed as a ratio of building frontage.",
    ),
    _location(
        SignType.FASCIA,
        Dimension.HEIGHT,
        ("fascia sign height", "wall sign maximum height", "projection above roof"),
        _LENGTH_UNITS,
    ),
    _location(
        SignType.FASCIA,
        Dimension.ILLUMINATION,
        ("illuminated fascia sign", "illumination", "internally illuminated"),
    ),
    _location(SignType.FASCIA, Dimension.PERMIT, ("sign permit required", "permit")),
    # --- channel letters -----------------------------------------------------
    _location(
        SignType.CHANNEL_LETTER,
        Dimension.AREA,
        ("channel letter", "individual letters", *_FASCIA_TERMS),
        _AREA_UNITS,
        ratio=True,
    ),
    _location(
        SignType.CHANNEL_LETTER,
        Dimension.ILLUMINATION,
        ("channel letter illumination", "internally illuminated", "halo lit"),
    ),
    _location(SignType.CHANNEL_LETTER, Dimension.PERMIT, ("sign permit required", "permit")),
    # --- pylon and freestanding ----------------------------------------------
    _location(
        SignType.PYLON,
        Dimension.AREA,
        ("pylon sign", "freestanding sign area", "maximum sign area"),
        _AREA_UNITS,
    ),
    _location(
        SignType.PYLON,
        Dimension.HEIGHT,
        ("pylon sign height", "maximum height", "freestanding sign height"),
        _LENGTH_UNITS,
    ),
    _location(
        SignType.PYLON,
        Dimension.SETBACK,
        ("setback", "property line", "minimum distance from"),
        _LENGTH_UNITS,
    ),
    _location(SignType.PYLON, Dimension.PERMIT, ("sign permit required", "permit")),
    _location(
        SignType.FREESTANDING,
        Dimension.AREA,
        ("freestanding sign", "ground sign", "maximum sign area"),
        _AREA_UNITS,
    ),
    _location(
        SignType.FREESTANDING,
        Dimension.HEIGHT,
        ("freestanding sign height", "ground sign height", "maximum height"),
        _LENGTH_UNITS,
    ),
    _location(
        SignType.FREESTANDING,
        Dimension.SETBACK,
        ("setback", "property line", "sight triangle"),
        _LENGTH_UNITS,
    ),
    _location(SignType.FREESTANDING, Dimension.PERMIT, ("sign permit required", "permit")),
    # --- window --------------------------------------------------------------
    _location(
        SignType.WINDOW,
        Dimension.AREA,
        ("window sign", "percentage of window area", "window coverage"),
        ("%", "m2", "ft2"),
        notes="Commonly a percentage of glazing rather than an absolute area.",
    ),
    _location(SignType.WINDOW, Dimension.PERMIT, ("window sign permit", "exempt")),
    # --- digital -------------------------------------------------------------
    _location(
        SignType.DIGITAL,
        Dimension.AREA,
        ("digital sign", "electronic message", "changeable copy"),
        _AREA_UNITS,
    ),
    _location(
        SignType.DIGITAL,
        Dimension.ILLUMINATION,
        ("brightness", "nits", "luminance", "dwell time", "message duration"),
        notes="Digital sign rules constrain brightness and dwell time as well as size.",
    ),
    _location(SignType.DIGITAL, Dimension.PERMIT, ("digital sign permit", "permit")),
    # --- awning, projecting, canopy -----------------------------------------
    _location(
        SignType.AWNING,
        Dimension.AREA,
        ("awning sign", "canopy sign", "maximum sign area"),
        _AREA_UNITS,
    ),
    _location(
        SignType.PROJECTING,
        Dimension.AREA,
        ("projecting sign", "blade sign", "maximum sign area"),
        _AREA_UNITS,
    ),
    _location(
        SignType.PROJECTING,
        Dimension.SETBACK,
        ("projection over", "encroachment", "clearance above"),
        _LENGTH_UNITS,
    ),
    _location(
        SignType.CANOPY,
        Dimension.AREA,
        ("canopy sign", "marquee sign", "maximum sign area"),
        _AREA_UNITS,
    ),
)


#: Municipality-specific vocabulary. Still only search terms — a municipality
#: whose numbers differ needs no entry here, because the numbers are never
#: stored anywhere.
MUNICIPALITY_OVERRIDES: dict[str, dict[tuple[SignType, Dimension], tuple[str, ...]]] = {}


def locations_for(
    sign_type: SignType, municipality_slug: str | None = None
) -> tuple[RuleLocation, ...]:
    """Rule locations for one sign type, with any municipality overrides."""
    overrides = MUNICIPALITY_OVERRIDES.get(municipality_slug or "", {})

    resolved: list[RuleLocation] = []
    for location in RULE_LOCATIONS:
        if location.sign_type is not sign_type:
            continue

        terms = overrides.get((location.sign_type, location.dimension))
        resolved.append(
            RuleLocation(
                sign_type=location.sign_type,
                dimension=location.dimension,
                search_terms=terms or location.search_terms,
                expected_units=location.expected_units,
                is_ratio_of_frontage=location.is_ratio_of_frontage,
                notes=location.notes,
            )
        )

    return tuple(resolved)
