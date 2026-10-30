# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""create schedule items table with btree_gist conflict detection

Revision ID: 0003_create_schedule_items_table
Revises: 0002_add_asset_ingest_fields
Create Date: 2026-05-09

Sprint 0.3 task 4 — premiere/embargo scheduling with DB-level conflict
detection per the spec's Schedule lifecycle section (premiere / embargo
semantics, channel conflict rules). The "§1070" reference from earlier
drafts predates the current spec numbering and does not match a section
in docs/spec/spec.md — see DOC-010 in
audit-civiccast-v0.3.0-2026-05-10/03-documentation-deepdive.md.

The schedule_items table connects ``channel x asset x airtime`` and
carries a mode (live | premiere | embargo) plus a state (scheduled |
cancelled | published).

Conflict detection (Postgres only):

  - The btree_gist extension is required for the EXCLUDE constraint.
  - The constraint partitions by ``channel_id`` (equality) AND uses
    GiST overlap (``&&``) on the time range
    ``tstzrange(scheduled_at, scheduled_at + duration_seconds * interval '1 second')``.
  - The WHERE clause filters to ``mode IN ('live', 'premiere') AND
    state = 'scheduled'`` so embargo entries (single-moment publishes,
    Schedule lifecycle section) and cancelled/published items never trip the check.

SQLite path:

  - SQLite has no btree_gist + EXCLUDE support. The CHECK constraints on
    mode, state, and duration↔mode coupling DO ship via the SA model's
    ``__table_args__`` so ``Base.metadata.create_all`` produces a
    table with those rules; the EXCLUDE conflict-detection contract is
    asserted exclusively against the real-Postgres testcontainers
    fixture (per ADR 0008's SQLite-vs-Postgres divergence note).

Per ADR 0008: both ``upgrade`` and ``downgrade`` implemented and tested
(``tests/schedule/test_migration_reversibility.py`` + real-Postgres
coverage in ``tests/schedule/test_real_postgres.py``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_create_schedule_items_table"
down_revision: str | None = "0002_add_asset_ingest_fields"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    """Return True when the active dialect supports schemas (Postgres)."""
    return op.get_bind().dialect.name != "sqlite"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Create civiccast.schedule_items + (Postgres) btree_gist EXCLUDE."""
    schema = "civiccast" if _use_schema() else None

    # 1. Postgres: ensure the btree_gist extension exists. Idempotent.
    if _is_postgres():
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # 2. Create the table with the structural CHECK constraints. The
    #    EXCLUDE conflict-detection constraint is added separately via
    #    raw SQL on Postgres only.
    op.create_table(
        "schedule_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("channel_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column(
            "state",
            sa.String(16),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        # Denormalized end timestamp — stored to side-step Postgres'
        # IMMUTABLE-expression requirement on EXCLUDE constraints.
        # ``timestamptz + interval`` is STABLE not IMMUTABLE; storing
        # the computed end as a plain column lets the EXCLUDE constraint
        # reference two columns directly. Store layer keeps this in
        # sync with ``scheduled_at + duration_seconds`` at write time.
        sa.Column(
            "scheduled_at_end",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "mode IN ('live', 'premiere', 'embargo')",
            name="schedule_items_mode_check",
        ),
        sa.CheckConstraint(
            "state IN ('scheduled', 'cancelled', 'published')",
            name="schedule_items_state_check",
        ),
        sa.CheckConstraint(
            # Explicit IS NOT NULL is load-bearing — see the matching
            # SA __table_args__ comment in civiccast.schedule.models.
            "(mode = 'embargo' AND duration_seconds IS NULL) OR "
            "(mode IN ('live', 'premiere') "
            "AND duration_seconds IS NOT NULL "
            "AND duration_seconds > 0)",
            name="schedule_items_duration_matches_mode",
        ),
        schema=schema,
    )

    # 3. Lookup index on channel_id + scheduled_at for the operator
    #    library's "events on this channel between X and Y" queries
    #    (Sprint 0.4+ uses this; cheap to ship now).
    op.create_index(
        "schedule_items_channel_scheduled_idx",
        "schedule_items",
        ["channel_id", "scheduled_at"],
        schema=schema,
    )

    # 4. Postgres: add the btree_gist EXCLUDE constraint. The raw SQL
    #    expresses the time range as a tstzrange constructed from the
    #    scheduled_at + duration_seconds. The WHERE clause skips embargo
    #    rows (no duration to overlap) and cancelled/published rows (no
    #    longer occupying the slot).
    if _is_postgres():
        # Postgres requires expressions used in indexes and EXCLUDE
        # constraints to be IMMUTABLE. ``timestamptz + interval`` is
        # STABLE (the result depends on session TIME ZONE), and
        # ``make_interval(secs => ...)`` is also non-immutable. Both
        # would fail with "functions in index expression must be marked
        # IMMUTABLE." We store ``scheduled_at_end`` as a plain column
        # (the store keeps it in sync at write time) so the EXCLUDE
        # constraint can reference two columns directly.
        op.execute(
            """
            ALTER TABLE civiccast.schedule_items
            ADD CONSTRAINT schedule_items_no_overlap
            EXCLUDE USING gist (
                channel_id WITH =,
                tstzrange(scheduled_at, scheduled_at_end) WITH &&
            )
            WHERE (
                mode IN ('live', 'premiere')
                AND state = 'scheduled'
            )
            """
        )


def downgrade() -> None:
    """Drop schedule_items + extension on the Postgres path."""
    schema = "civiccast" if _use_schema() else None

    op.drop_index(
        "schedule_items_channel_scheduled_idx",
        table_name="schedule_items",
        schema=schema,
    )
    op.drop_table("schedule_items", schema=schema)

    # Leave the btree_gist extension in place on downgrade — the
    # extension is shared infrastructure, and dropping it could break
    # other Postgres usage. Database admins remove extensions explicitly.
