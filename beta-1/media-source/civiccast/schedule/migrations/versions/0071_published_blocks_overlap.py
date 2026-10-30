# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Published schedule items now block overlapping inserts (Commit-to-Air
enforcement follow-up, owner decision 2026-07-08).

Revision ID: 0071_published_blocks_overlap
Revises: 0070_grandfather_scheduled_to_published
Create Date: 2026-07-08

Migration 0070 flipped every pre-existing ``scheduled`` schedule_items row
to ``published`` at upgrade time, so Commit-to-Air enforcement wouldn't
silently stop an already-approved on-air schedule. But the
``schedule_items_no_overlap`` EXCLUDE constraint (rebuilt by migration
0005) still only fires ``WHERE state = 'scheduled' AND mode = 'premiere'``
— a ``published`` item occupies real airtime (it airs; see
``civiccast.egress.source_plan.build_source_plan_from_schedule``) but no
longer participates in overlap-conflict detection once it transitions out
of ``scheduled``. A new scheduled item can be inserted directly on top of
an already-approved, airing published program with no conflict rejection.

This migration rebuilds the EXCLUDE so its WHERE clause is
``state IN ('scheduled', 'published') AND mode = 'premiere'`` — the
published state now blocks overlapping inserts exactly the way scheduled
always has. Downgrade restores the scheduled-only predicate from
migration 0005.

Postgres-only (gated behind ``_use_schema()``, same as 0005); SQLite
cannot enforce the btree_gist EXCLUDE constraint and is a documented
no-op — see ``tests/schedule/test_schedule_store.py`` module docstring.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0071_published_blocks_overlap"
down_revision: str | None = "0070_grandfather_scheduled_to_published"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    """Widen the EXCLUDE WHERE clause to also cover ``published`` rows."""
    if not _use_schema():
        return

    op.execute("ALTER TABLE civiccast.schedule_items DROP CONSTRAINT schedule_items_no_overlap")
    op.execute(
        """
        ALTER TABLE civiccast.schedule_items
        ADD CONSTRAINT schedule_items_no_overlap EXCLUDE USING gist (
            channel_id WITH =,
            tstzrange(scheduled_at, scheduled_at_end) WITH &&
        )
        WHERE (
            state IN ('scheduled', 'published')
            AND mode = 'premiere'
            AND scheduled_at_end IS NOT NULL
        )
        """
    )


def downgrade() -> None:
    """Restore the migration-0005 scheduled-only predicate."""
    if not _use_schema():
        return

    op.execute("ALTER TABLE civiccast.schedule_items DROP CONSTRAINT schedule_items_no_overlap")
    op.execute(
        """
        ALTER TABLE civiccast.schedule_items
        ADD CONSTRAINT schedule_items_no_overlap EXCLUDE USING gist (
            channel_id WITH =,
            tstzrange(scheduled_at, scheduled_at_end) WITH &&
        )
        WHERE (
            state = 'scheduled'
            AND mode = 'premiere'
            AND scheduled_at_end IS NOT NULL
        )
        """
    )
