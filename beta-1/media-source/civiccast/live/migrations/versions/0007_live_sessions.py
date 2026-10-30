# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""create live_sessions, live_sources, recording_targets

Revision ID: 0007_live_sessions
Revises: 0006_widen_asset_state_check
Create Date: 2026-05-11

Sprint 0.4 Slice 1 Commit 3 - data spine for the live-broadcast
module. Creates three new tables in the ``civiccast`` schema:

- ``live_sessions`` - the live broadcast session entity with state
  machine values ``idle / preflight / on_air / ending / recorded``
  matching the SA model in ``civiccast/live/models.py``.
- ``live_sources`` - configured input sources, source_type one of
  ``rtmp / rtsp / ndi / srt`` (SDI is post-1.0 cable add-on territory
  per spec section 8.3).
- ``recording_targets`` - filesystem path or object-store URI for
  finalized recordings.

This migration lives in the ``civiccast/live/`` per-module Alembic
versions directory. The single Alembic runner walks every per-module
migration graph because each directory is registered in
``alembic.ini``'s ``version_locations`` list - ``ScriptDirectory`` is
built from that ini list before ``env.py`` runs, so ini registration
is load-bearing. ``alembic/env.py:discover_version_locations`` also
resolves the same paths at runtime as a defense-in-depth helper. ADR
0008 documents the per-module-migrations-under-one-env pattern.

Per ADR 0008: both upgrade and downgrade implemented. Downgrade drops
the three new tables. No cross-table FKs to existing schedule.assets
in this commit; the finalization commit (later in Slice 1) will add
the asset transition path.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_live_sessions"
down_revision: str | None = "0006_widen_asset_state_check"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _server_default_now() -> sa.sql.elements.TextClause:
    """Return the dialect-appropriate now() default.

    Postgres: ``now()``. SQLite: ``CURRENT_TIMESTAMP`` (Postgres's
    ``now()`` function does not exist on SQLite; the test path runs
    only the SA __table_args__ via create_all in any case, but the
    migration body keeps both dialects honest).
    """
    if _use_schema():
        return sa.text("now()")
    return sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    """Create live_sessions, live_sources, recording_targets tables."""
    schema = "civiccast" if _use_schema() else None
    now_default = _server_default_now()

    op.create_table(
        "live_sessions",
        sa.Column("live_session_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="idle",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now_default,
        ),
        sa.PrimaryKeyConstraint("live_session_id", name="live_sessions_pkey"),
        sa.CheckConstraint(
            "state IN ('idle', 'preflight', 'on_air', 'ending', 'recorded')",
            name="live_sessions_state_check",
        ),
        schema=schema,
    )

    op.create_table(
        "live_sources",
        sa.Column("live_source_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=8), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("credentials_handle", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now_default,
        ),
        sa.PrimaryKeyConstraint("live_source_id", name="live_sources_pkey"),
        sa.CheckConstraint(
            "source_type IN ('rtmp', 'rtsp', 'ndi', 'srt')",
            name="live_sources_source_type_check",
        ),
        schema=schema,
    )

    op.create_table(
        "recording_targets",
        sa.Column("recording_target_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("target_uri", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now_default,
        ),
        sa.PrimaryKeyConstraint("recording_target_id", name="recording_targets_pkey"),
        schema=schema,
    )


def downgrade() -> None:
    """Drop the three Slice 1 Commit 3 tables.

    No data-existence guard at this stage; the tables are introduced in
    this rung and contain no operator-side data through Slice 1.
    Finalization (later Slice 1 commits) will add data, at which point
    a future migration's downgrade may want a guard analogous to the
    one in 0006_widen_asset_state_check.
    """
    schema = "civiccast" if _use_schema() else None
    op.drop_table("recording_targets", schema=schema)
    op.drop_table("live_sources", schema=schema)
    op.drop_table("live_sessions", schema=schema)
