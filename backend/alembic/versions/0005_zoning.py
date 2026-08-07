"""Zoning lookup: GIS configuration and a parcel cache.

Adds:
  * GIS provider configuration on ``municipality``, so adding a city is a row
    rather than a code change
  * ``parcel_zoning``, caching what a municipality's open data said about a
    parcel, with the source URL and an expiry

**Geometry is a JSONB reference, not a geometry column.** Nothing in the sign
bylaw work needs spatial arithmetic — a parcel's zone is looked up by address or
parcel number, not by intersecting polygons. Storing the provider's own
geometry reference keeps the door open for PostGIS later without requiring the
extension now, and without pretending to a spatial precision we do not have.

**Everything here is a cache, and cached zoning goes stale.** A rezoning changes
the answer, so rows carry ``fetched_at`` and ``expires_at`` and the interface
states the as-at date. A confidently wrong zone produces a confidently wrong
sign rule, which is the failure this whole system is built to avoid.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- municipality GIS configuration --------------------------------------
    op.add_column(
        "municipality", sa.Column("gis_provider", sa.String(length=60), nullable=True)
    )
    op.add_column(
        "municipality", sa.Column("gis_endpoint", sa.String(length=1000), nullable=True)
    )
    # Where a human can check the answer themselves. Every zoning result links
    # here, because an automated lookup a user cannot verify is worth little.
    op.add_column(
        "municipality", sa.Column("map_url", sa.String(length=1000), nullable=True)
    )

    # --- parcel zoning cache -------------------------------------------------
    op.create_table(
        "parcel_zoning",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("municipality_id", sa.Uuid(), nullable=False),
        # As the user typed it, for display.
        sa.Column("address", sa.String(length=500), nullable=False),
        # Casefolded and punctuation-stripped, for the cache key. Two spellings
        # of one address must not produce two lookups and two answers.
        sa.Column("normalized_address", sa.String(length=500), nullable=False),
        sa.Column("parcel_number", sa.String(length=80), nullable=True),
        sa.Column("legal_description", sa.Text(), nullable=True),
        sa.Column("zoning_code", sa.String(length=40), nullable=True),
        sa.Column("zoning_description", sa.String(length=500), nullable=True),
        sa.Column(
            "geometry_reference",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("provider", sa.String(length=60), nullable=False),
        sa.Column(
            "confidence", sa.Float(), server_default=sa.text("0.0"), nullable=False
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["municipality_id"],
            ["municipality.id"],
            name="fk_parcel_zoning_municipality_id_municipality",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_parcel_zoning"),
        sa.UniqueConstraint(
            "municipality_id",
            "normalized_address",
            name="uq_parcel_zoning_municipality_address",
        ),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )
    op.create_index("ix_parcel_zoning_municipality_id", "parcel_zoning", ["municipality_id"])
    op.create_index("ix_parcel_zoning_parcel_number", "parcel_zoning", ["parcel_number"])
    op.create_index("ix_parcel_zoning_expires_at", "parcel_zoning", ["expires_at"])


def downgrade() -> None:
    op.drop_table("parcel_zoning")
    for column in ("map_url", "gis_endpoint", "gis_provider"):
        op.drop_column("municipality", column)
