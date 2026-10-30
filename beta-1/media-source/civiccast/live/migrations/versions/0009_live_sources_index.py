# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""index live_sources.channel_id

Revision ID: 0009_live_sources_index
Revises: 0008_finalization_spine
Create Date: 2026-05-11

Audit-team v0.4 Slice 1 ENG-004 hardening. Adds a single btree index on
``live_sources.channel_id`` to close the hot-path scan the audit
flagged on the operator-side ``LiveSourceStore.list_for_channel()``
query (``WHERE channel_id = ?``).

The base table was created in migration ``0007_live_sessions`` without
an index on ``channel_id``; at Slice 1 cardinality (handful of sources
per channel) a sequential scan is fine, but the audit-team's blast-
radius read flagged this as a class of query that will compound as the
live module grows. Five-line migration; downgrade drops the index.

Per ADR 0008: both upgrade and downgrade implemented.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_live_sources_index"
down_revision: str | None = "0008_finalization_spine"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    """Create ``ix_live_sources_channel_id`` on ``live_sources(channel_id)``."""
    schema = "civiccast" if _use_schema() else None
    op.create_index(
        "ix_live_sources_channel_id",
        "live_sources",
        ["channel_id"],
        schema=schema,
    )


def downgrade() -> None:
    """Drop ``ix_live_sources_channel_id``.

    No data guard required -- dropping a btree index does not lose
    operator data; it only restores the pre-0009 scan behavior.
    """
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_live_sources_channel_id",
        table_name="live_sources",
        schema=schema,
    )
