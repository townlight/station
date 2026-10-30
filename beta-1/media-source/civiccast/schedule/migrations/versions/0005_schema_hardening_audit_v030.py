# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Schema hardening — audit-team v0.3.0 findings

Revision ID: 0005_schema_hardening_audit_v030
Revises: 0004_asset_trim_chapters_meta
Create Date: 2026-05-10

Closes a batch of audit-team findings against the v0.3.0 schema:

  - **ENG-004** — drop ``mode = 'live'`` from ``schedule_items``. Live mode
    was in the schema/CHECK/EXCLUDE WHERE through v0.3.0 but had no end-
    to-end wiring (no live source descriptor; the operator drawer never
    offered it). The Sprint 0.5 live-ingest rung will design from scratch.
    Removed from the mode CHECK and rebuilt the EXCLUDE WHERE clause.

  - **ENG-005** — ``scheduled_at_end`` had no DB trigger. Today the column
    is maintained by the Python side at write time, which is correct as
    long as Python is the only writer. The moment a future reschedule
    endpoint or a direct-SQL operational tool issues an UPDATE that
    changes ``scheduled_at`` or ``duration_seconds`` and forgets to update
    ``scheduled_at_end``, the EXCLUDE constraint silently uses a stale
    end time for conflict detection — false negatives. This migration
    adds a Postgres BEFORE INSERT OR UPDATE trigger that recomputes
    ``scheduled_at_end`` from ``scheduled_at + duration_seconds * interval
    '1 second'`` on every write.

  - **ENG-010** — ``assets_trim_out_positive`` CHECK. Pydantic gates
    ``trim_out_seconds`` at the API surface (``ge=1``); the DB CHECK
    was missing. Defense-in-depth at the column for any future writer
    that bypasses Pydantic.

  - **QA-008** — ``assets.version`` column for optimistic concurrency on
    ``PATCH /api/staff/assets/{id}``. Without this, two operators editing
    the same asset's chapters in different tabs both write a full
    chapter list and the second saver silently overwrites the first.
    The PATCH path now requires the client to echo back the version it
    last observed; mismatch surfaces as 409. Defaults to 1 for existing
    rows; increments on each successful update.

  - **QA-010** — ``schedule_items.duration_seconds`` upper bound, baked
    into the rebuilt ``schedule_items_duration_matches_mode`` CHECK at
    14 days (1_209_600 seconds). Prevents a typo from landing a 23-month
    schedule item that blocks the channel for the foreseeable future.

Per ADR 0008: both ``upgrade`` and ``downgrade`` implemented.

SQLite test paths get all of these via the SA model's ``__table_args__``
at ``create_all`` time. The Postgres-specific trigger function and the
EXCLUDE rebuild are gated behind ``_use_schema()``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_schema_hardening_audit_v030"
down_revision: str | None = "0004_asset_trim_chapters_meta"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    """Apply the audit-team v0.3.0 schema hardening batch."""
    schema = "civiccast" if _use_schema() else None

    # -----------------------------------------------------------------------
    # assets.version — QA-008
    # -----------------------------------------------------------------------
    op.add_column(
        "assets",
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        schema=schema,
    )

    # -----------------------------------------------------------------------
    # assets_trim_out_positive CHECK — ENG-010
    # -----------------------------------------------------------------------
    if _use_schema():
        op.create_check_constraint(
            "assets_trim_out_positive",
            "assets",
            "trim_out_seconds IS NULL OR trim_out_seconds >= 1",
            schema=schema,
        )

    # -----------------------------------------------------------------------
    # schedule_items: drop live + duration upper bound — ENG-004 + QA-010
    # -----------------------------------------------------------------------
    if _use_schema():
        # Postgres can't ALTER a CHECK in place; drop and recreate.
        op.drop_constraint(
            "schedule_items_mode_check",
            "schedule_items",
            type_="check",
            schema=schema,
        )
        op.drop_constraint(
            "schedule_items_duration_matches_mode",
            "schedule_items",
            type_="check",
            schema=schema,
        )
        op.create_check_constraint(
            "schedule_items_mode_check",
            "schedule_items",
            "mode IN ('premiere', 'embargo')",
            schema=schema,
        )
        op.create_check_constraint(
            "schedule_items_duration_matches_mode",
            "schedule_items",
            "(mode = 'embargo' AND duration_seconds IS NULL) OR "
            "(mode = 'premiere' "
            "AND duration_seconds IS NOT NULL "
            "AND duration_seconds > 0 "
            "AND duration_seconds <= 1209600)",
            schema=schema,
        )

        # Rebuild the EXCLUDE so its WHERE clause no longer references 'live'.
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

        # -----------------------------------------------------------------------
        # ENG-005 — scheduled_at_end trigger
        # -----------------------------------------------------------------------
        op.execute(
            """
            CREATE OR REPLACE FUNCTION civiccast.schedule_items_set_end()
            RETURNS trigger LANGUAGE plpgsql AS $func$
            BEGIN
              IF NEW.duration_seconds IS NULL THEN
                NEW.scheduled_at_end := NULL;
              ELSE
                NEW.scheduled_at_end :=
                  NEW.scheduled_at + (NEW.duration_seconds || ' seconds')::interval;
              END IF;
              RETURN NEW;
            END;
            $func$;
            """
        )
        op.execute(
            """
            CREATE TRIGGER schedule_items_set_end_trg
            BEFORE INSERT OR UPDATE OF scheduled_at, duration_seconds
            ON civiccast.schedule_items
            FOR EACH ROW
            EXECUTE FUNCTION civiccast.schedule_items_set_end();
            """
        )


def downgrade() -> None:
    """Reverse every change applied by upgrade()."""
    schema = "civiccast" if _use_schema() else None

    if _use_schema():
        # Trigger + function — ENG-005
        op.execute("DROP TRIGGER IF EXISTS schedule_items_set_end_trg ON civiccast.schedule_items")
        op.execute("DROP FUNCTION IF EXISTS civiccast.schedule_items_set_end()")

        # Restore the v0.4-era EXCLUDE WHERE clause that included 'live'.
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
                AND mode IN ('live', 'premiere')
                AND scheduled_at_end IS NOT NULL
            )
            """
        )

        # Restore the v0.4-era CHECK constraints (with 'live' + no upper cap).
        op.drop_constraint(
            "schedule_items_duration_matches_mode",
            "schedule_items",
            type_="check",
            schema=schema,
        )
        op.drop_constraint(
            "schedule_items_mode_check",
            "schedule_items",
            type_="check",
            schema=schema,
        )
        op.create_check_constraint(
            "schedule_items_mode_check",
            "schedule_items",
            "mode IN ('live', 'premiere', 'embargo')",
            schema=schema,
        )
        op.create_check_constraint(
            "schedule_items_duration_matches_mode",
            "schedule_items",
            "(mode = 'embargo' AND duration_seconds IS NULL) OR "
            "(mode IN ('live', 'premiere') "
            "AND duration_seconds IS NOT NULL "
            "AND duration_seconds > 0)",
            schema=schema,
        )

        # ENG-010
        op.drop_constraint(
            "assets_trim_out_positive",
            "assets",
            type_="check",
            schema=schema,
        )

    # QA-008
    op.drop_column("assets", "version", schema=schema)
