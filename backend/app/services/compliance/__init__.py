"""Sign compliance checking, backed by retrieval rather than coded rules.

    proposed sign -> retrieve governing section -> parse limit -> compare
                  -> verdict + the citation it was computed from

No municipal regulation is written in this package. Rule *locations* — search
terms and expected units — are configuration; the numbers are read from indexed
bylaw text at question time. When a bylaw is amended, a stale rule location
fails visibly instead of answering wrongly.
"""

from __future__ import annotations

from app.services.compliance.base import (
    ComplianceCheck,
    ComplianceOutcome,
    ComplianceReport,
    Dimension,
    MeasuredValue,
    RuleLocation,
    SignSpec,
    SignType,
)
from app.services.compliance.engine import ComplianceEngine
from app.services.compliance.parsing import NumericLimit, extract_limit
from app.services.compliance.rules import RULE_LOCATIONS, locations_for

__all__ = [
    "RULE_LOCATIONS",
    "ComplianceCheck",
    "ComplianceEngine",
    "ComplianceOutcome",
    "ComplianceReport",
    "Dimension",
    "MeasuredValue",
    "NumericLimit",
    "RuleLocation",
    "SignSpec",
    "SignType",
    "extract_limit",
    "locations_for",
]
