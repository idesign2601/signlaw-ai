"""Extracting numeric limits from bylaw prose.

The riskiest code in the subsystem, because its failure mode is a plausible
number rather than an error. Written to be conservative in one specific way:
**an ambiguous sentence yields nothing.**

Bylaw text is not consistent. All of these appear:

    "shall not exceed 9.3 square metres"
    "maximum sign area: 0.2 m² per metre of building frontage"
    "no greater than 7.5 m in height"
    "20% of the window area"

And these, which must *not* produce a limit:

    "as set out in Schedule B"
    "shall not exceed the area permitted in Section 4.2"
    "between 2.4 and 4.5 metres"      (a range, not a maximum)

A returned number is always accompanied by the sentence it came from, so a
reviewer can see what was read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["NumericLimit", "extract_limit"]


# Unit spellings seen in BC and Alberta bylaws, mapped to a canonical form.
_UNITS: dict[str, str] = {
    "square metre": "m2",
    "square metres": "m2",
    "square meter": "m2",
    "square meters": "m2",
    "sq m": "m2",
    "sq. m": "m2",
    "m2": "m2",
    "m²": "m2",
    "metre": "m",
    "metres": "m",
    "meter": "m",
    "meters": "m",
    "m": "m",
    "square foot": "ft2",
    "square feet": "ft2",
    "sq ft": "ft2",
    "ft2": "ft2",
    "foot": "ft",
    "feet": "ft",
    "ft": "ft",
    "percent": "%",
    "per cent": "%",
    "%": "%",
}

_UNIT_ALTERNATION = "|".join(re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True))

# "shall not exceed 9.3 square metres", "maximum of 7.5 m"
_LIMIT = re.compile(
    r"(?:shall\s+not\s+exceed|must\s+not\s+exceed|no\s+greater\s+than|"
    r"not\s+more\s+than|maximum(?:\s+of)?|up\s+to|no\s+more\s+than)"
    r"[^0-9]{0,40}?"
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    rf"(?P<unit>{_UNIT_ALTERNATION})\b",
    re.IGNORECASE,
)

# "0.2 m² per metre of frontage" — a ratio, not a flat limit.
_RATIO = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*"
    rf"(?P<unit>{_UNIT_ALTERNATION})\s*"
    r"(?:per|for\s+each|/)\s*"
    r"(?:(?:lineal|linear)\s+)?"
    r"(?:metre|meter|m|foot|ft)\b[^.]{0,40}?"
    r"(?:frontage|width|store\s*front|building\s+face)",
    re.IGNORECASE,
)

# Sentences that name a limit without stating it. A number appearing nearby
# belongs to something else, and reading it would be worse than reading nothing.
_DEFERRAL = re.compile(
    r"\b(?:as\s+set\s+out\s+in|in\s+accordance\s+with|specified\s+in|"
    r"as\s+provided\s+in|pursuant\s+to|refer\s+to)\b[^.]{0,60}?"
    r"\b(?:schedule|section|table|part|appendix)\b",
    re.IGNORECASE,
)

# "between 2.4 and 4.5 metres" — a range has no single maximum.
_RANGE = re.compile(r"\bbetween\s+\d+(?:[.,]\d+)?\s*(?:\w+)?\s+and\s+\d+", re.IGNORECASE)

_SENTENCE = re.compile(r"(?<=[.;])\s+")


@dataclass(frozen=True, slots=True)
class NumericLimit:
    """A limit read out of one sentence."""

    value: float
    unit: str
    sentence: str
    is_ratio_of_frontage: bool = False


def extract_limit(text: str, *, expected_units: tuple[str, ...] = ()) -> NumericLimit | None:
    """Find the governing numeric limit in a passage, or ``None``.

    Sentences are examined individually so a limit is always attributable to the
    text quoted beside it. The first usable sentence wins: bylaw sections state
    the general rule before the exceptions, and the exceptions carry their own
    conditions that this function has no way to evaluate.
    """
    for sentence in _sentences(text):
        if _DEFERRAL.search(sentence) or _RANGE.search(sentence):
            # Says a limit exists elsewhere, or gives a range. Both mean the
            # number in this sentence is not the answer.
            continue

        ratio = _RATIO.search(sentence)
        if ratio:
            limit = _build(ratio, sentence, is_ratio=True)
            if _acceptable(limit, expected_units):
                return limit

        match = _LIMIT.search(sentence)
        if match:
            limit = _build(match, sentence, is_ratio=False)
            if _acceptable(limit, expected_units):
                return limit

    return None


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE.split(text) if part.strip()]


def _build(match: re.Match[str], sentence: str, *, is_ratio: bool) -> NumericLimit | None:
    raw = match.group("value").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:  # pragma: no cover — the pattern guarantees digits
        return None

    unit = _UNITS.get(match.group("unit").strip().lower())
    if unit is None:
        return None

    return NumericLimit(
        value=value,
        unit=unit,
        sentence=sentence,
        is_ratio_of_frontage=is_ratio,
    )


def _acceptable(limit: NumericLimit | None, expected_units: tuple[str, ...]) -> bool:
    """Reject a limit in the wrong unit.

    A height rule that parses "9.3 square metres" has matched the area sentence
    instead. Comparing that to a proposed height would produce a verdict from
    two unrelated quantities.
    """
    if limit is None:
        return False
    return not expected_units or limit.unit in expected_units
