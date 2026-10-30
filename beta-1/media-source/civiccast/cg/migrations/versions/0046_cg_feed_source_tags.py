# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add cg_feed_sources.tags (CivicCast 3.0 — S18 gap 6 CG depth, build step 7).

Revision ID: 0046_cg_feed_source_tags
Revises: 0045_cg_depth
Create Date: 2026-06-16

The feed fetcher stamps a feed source's tags onto each of its items so a board
zone with ``allowed_tags`` can include them (DC-CG3 live path). Single global
chain (ADR 0008); parents on the real head ``0045_cg_depth``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_cg_feed_source_tags"
down_revision = "0045_cg_depth"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "cg_feed_sources",
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("cg_feed_sources", "tags", schema=schema)
