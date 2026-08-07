"""Known starting configurations for municipal zoning services.

**Nothing reads this at runtime.** These are suggestions an operator can load
into the admin form and then verify; the live configuration is the
``gis_provider`` / ``gis_endpoint`` / ``gis_config`` columns on ``municipality``.

Every entry is marked with whether it has been confirmed against the city's own
service directory. Unconfirmed entries have no endpoint, deliberately:

An ArcGIS layer that responds but carries a *different* field for the zone
returns nothing, harmlessly. One that carries a *similar* field returns a
confidently wrong zone, and the API responds happily either way. Searching for
Vancouver's zoning API, for instance, surfaces an ArcGIS hub with a "parcel
zoning" dataset belonging to Vancouver, **Washington** — plausible, well-formed,
and the wrong country. Endpoints are therefore verified per municipality, never
inferred from a name.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PRESETS", "preset_for"]


PRESETS: dict[str, dict[str, Any]] = {
    # --- British Columbia ----------------------------------------------------
    "vancouver": {
        "verified": False,
        "kind": "opendatasoft",
        "endpoint": "https://opendata.vancouver.ca",
        "map_url": "https://maps.vancouver.ca/zoning/",
        "config": {
            "dataset": "zoning-districts-and-labels",
            "fields": {
                "zoning_code": "zoning_district",
                "zoning_description": "zoning_classification",
                # Polygons: no address field. A geocoder is required, and
                # without one the provider reports itself unconfigured rather
                # than returning an arbitrary polygon.
            },
        },
        "notes": (
            "Dataset confirmed to exist and refresh weekly. Address resolution "
            "needs a geocoder because the dataset is polygons."
        ),
    },
    "surrey": {
        "verified": False,
        "kind": "arcgis",
        "endpoint": None,
        "map_url": "https://cosmos.surrey.ca/external/",
        "config": {
            "fields": {
                "zoning_code": "ZONE",
                "zoning_description": "ZONE_DESC",
                "address": "CIVIC_ADDRESS",
                "parcel_number": "PID",
            }
        },
        "notes": "Field names unverified. Confirm against Surrey's REST directory.",
    },
    "burnaby": {
        "verified": False,
        "kind": "arcgis",
        "endpoint": None,
        "map_url": "https://gis.burnaby.ca/",
        "config": {
            "fields": {
                "zoning_code": "ZONING",
                "zoning_description": "ZONING_DESCRIPTION",
                "address": "ADDRESS",
                "parcel_number": "PID",
            }
        },
        "notes": "Field names unverified.",
    },
    "richmond": {
        "verified": False,
        "kind": "arcgis",
        "endpoint": None,
        "map_url": "https://maps.richmond.ca/",
        "config": {"fields": {"zoning_code": "ZONING", "address": "ADDRESS"}},
        "notes": "Field names unverified.",
    },
    "coquitlam": {
        "verified": False,
        "kind": "arcgis",
        "endpoint": None,
        "map_url": "https://www.coquitlam.ca/",
        "config": {"fields": {"zoning_code": "ZONE", "address": "ADDRESS"}},
        "notes": "Field names unverified.",
    },
    # --- Alberta -------------------------------------------------------------
    # The expansion test. Adding these required no Python change: a kind, an
    # endpoint and a field mapping.
    "ab-calgary": {
        "verified": False,
        "kind": "socrata",
        "endpoint": "https://data.calgary.ca",
        "map_url": "https://maps.calgary.ca/PropertyAssessment/",
        "config": {
            "dataset": "zoning",
            "fields": {"zoning_code": "land_use_designation", "address": "address"},
        },
        "notes": "Dataset identifier and field names unverified.",
    },
    "ab-edmonton": {
        "verified": False,
        "kind": "socrata",
        "endpoint": "https://data.edmonton.ca",
        "map_url": "https://maps.edmonton.ca/",
        "config": {
            "dataset": "zoning",
            "fields": {"zoning_code": "zoning", "address": "address"},
        },
        "notes": "Dataset identifier and field names unverified.",
    },
}


def preset_for(municipality_slug: str) -> dict[str, Any] | None:
    """A starting configuration for a municipality, if one is recorded."""
    return PRESETS.get(municipality_slug)
