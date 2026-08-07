"""Make zoning providers configuration rather than code.

``0005`` recorded a provider *name* per municipality, which meant a new city
needed a Python module supplying its field names. That is the wrong seam: a
field mapping is data.

``gis_provider`` now names the *kind* of service — ``arcgis``, ``opendatasoft``,
``socrata`` — and ``gis_config`` carries everything specific to the city: which
attribute holds the zone, which holds the address, dataset identifiers. Adding a
municipality becomes one row.

The kinds are code because each speaks a different query grammar. Cities are
not, because they differ only in vocabulary.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "municipality",
        sa.Column(
            "gis_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    # Whether the configuration has been checked against the city's own service
    # directory. An unverified endpoint is never queried: a layer that responds
    # with a similar-looking field returns a confidently wrong zone, which is
    # worse than returning nothing.
    op.add_column(
        "municipality",
        sa.Column(
            "gis_verified",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("municipality", "gis_verified")
    op.drop_column("municipality", "gis_config")
