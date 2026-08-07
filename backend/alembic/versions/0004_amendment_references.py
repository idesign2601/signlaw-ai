"""Persist the bylaw numbers a document amends.

``MetadataDetector`` already extracts these — "A Bylaw to amend Sign Bylaw No.
6163" — but they were never stored, so :class:`~app.ingestion.amendments.
LineageResolver` could not build amendment edges and ``bylaw_relation`` stayed
empty in every run.

Repeal references are deliberately not added yet: detection does not extract
them, and a column that is always empty is worse than no column, because it
reads as evidence of absence rather than absence of evidence.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document",
        sa.Column(
            "amends_bylaw_numbers",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("document", "amends_bylaw_numbers")
