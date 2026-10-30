# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S21 scheduled recording — sibling slot off ``0055_asrun_and_epg``.

This is the long-reserved ``0056`` slot per RECONCILIATION's chain-shape
footer. Until S21 shipped, the chain was a single linear path:

    0054 → 0055 → 0057 → 0058 → 0059

With S21 landing as a SIBLING off ``0055`` (declaring
``down_revision = "0055_asrun_and_epg"``), the chain forks at ``0055``:

    0054 → 0055 → 0056_scheduled_recording (this revision)
            \\___ → 0057 → 0058 → 0059

A merge revision (``0060_recording_paywall_merge``) in this same module
unifies the two heads — ``0056`` (this revision) and ``0059_paywall_access``
— so the global Alembic chain returns to a single head. The merge
revision is data-free: it only stamps that both heads have been observed.

Two tables for the net-new ``civiccast/recording/`` module:

* ``recording_schedules`` — one row per (station, name) capture
  schedule. A unique constraint on ``(station_id, name)`` enforces "no
  two schedules with the same operator-visible name at the same station"
  so the operator UI's picker is unambiguous.
* ``recording_jobs`` — one planned / running / completed capture. A
  CHECK constraint pins the ``state`` enum to the seven valid values; a
  composite index on ``(station_id, state)`` serves the operator UI's
  "what's running here?" view; an index on ``planned_start`` serves the
  scheduler's "what's due?" sweep.

The capture pipeline itself is wired at the service layer (slice 2) via
an injected Protocol; this migration is concerned only with persistence.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0056_scheduled_recording"
down_revision = "0055_asrun_and_epg"
branch_labels = None
depends_on = None

_SCHEDULES_TABLE = "recording_schedules"
_JOBS_TABLE = "recording_jobs"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    dialect = op.get_bind().dialect.name

    op.create_table(
        _SCHEDULES_TABLE,
        sa.Column("schedule_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("recurrence", sa.JSON(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("encoder_profile", sa.String(length=120), nullable=False),
        sa.Column(
            "loudness_regime",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'inherit'"),
        ),
        sa.Column("target_series", sa.String(length=120), nullable=True),
        # E-16 fix: ``custom_field_values`` was NOT NULL with no
        # ``server_default``. The ORM default=dict saved us at insert
        # time, but a raw SQL backfill / a psql audit insert that
        # omitted the column would have hit a NOT NULL violation.
        # ``'{}'`` is a portable JSON-empty literal that SQLite + PG
        # both accept.
        sa.Column(
            "custom_field_values",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("station_id", "name", name="recording_schedules_station_name_unique"),
        schema=schema,
    )
    op.create_index(
        "ix_recording_schedules_station",
        _SCHEDULES_TABLE,
        ["station_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_recording_schedules_enabled",
        _SCHEDULES_TABLE,
        ["enabled"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        _JOBS_TABLE,
        sa.Column("job_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("schedule_id", sa.String(length=120), nullable=True),
        sa.Column("planned_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'scheduled'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asset_id", sa.String(length=120), nullable=True),
        sa.Column(
            "bytes_written",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("encoder_profile", sa.String(length=120), nullable=False),
        sa.Column(
            "loudness_regime",
            sa.String(length=40),
            nullable=False,
            server_default=sa.text("'inherit'"),
        ),
        sa.Column("target_series", sa.String(length=120), nullable=True),
        # E-16 fix: same server_default treatment as ``recording_schedules``.
        sa.Column(
            "custom_field_values",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('scheduled', 'arming', 'recording', 'finalizing', "
            "'done', 'failed', 'skipped')",
            name="recording_jobs_state_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_recording_jobs_station_state",
        _JOBS_TABLE,
        ["station_id", "state"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_recording_jobs_planned_start",
        _JOBS_TABLE,
        ["planned_start"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_recording_jobs_schedule",
        _JOBS_TABLE,
        ["schedule_id"],
        unique=False,
        schema=schema,
    )

    # E-3 fix (Postgres only): partial unique index on
    # ``(station_id, source_identifier)`` filtered by
    # ``state IN ('arming', 'recording', 'finalizing')`` so two
    # concurrent sessions cannot both transition jobs targeting the
    # same source into ``arming``. The application also runs an
    # in-transaction overlap re-check (see
    # ``RecordingStore.transition_to_arming_with_overlap_guard``); the
    # DB-level index is defense in depth for the case where the two
    # sessions cross the wire at the exact same instant.
    #
    # SQLite cannot index a JSON-extract expression in a way that the
    # SA migration helper would emit portably, so this index is
    # PG-only. The application-level guard covers SQLite (which is the
    # test-only target).
    if dialect == "postgresql":
        schema_qualifier = f"{schema}." if schema else ""
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS "
                f"ux_recording_jobs_active_source ON {schema_qualifier}{_JOBS_TABLE} "
                f"(station_id, COALESCE(source_snapshot->>'uri', "
                f"source_snapshot->>'input_id', '')) "
                f"WHERE state IN ('arming', 'recording', 'finalizing')"
            )
        )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        schema_qualifier = f"{schema}." if schema else ""
        op.execute(
            sa.text(f"DROP INDEX IF EXISTS {schema_qualifier}ux_recording_jobs_active_source")
        )
    op.drop_index("ix_recording_jobs_schedule", table_name=_JOBS_TABLE, schema=schema)
    op.drop_index("ix_recording_jobs_planned_start", table_name=_JOBS_TABLE, schema=schema)
    op.drop_index("ix_recording_jobs_station_state", table_name=_JOBS_TABLE, schema=schema)
    op.drop_table(_JOBS_TABLE, schema=schema)

    op.drop_index("ix_recording_schedules_enabled", table_name=_SCHEDULES_TABLE, schema=schema)
    op.drop_index("ix_recording_schedules_station", table_name=_SCHEDULES_TABLE, schema=schema)
    op.drop_table(_SCHEDULES_TABLE, schema=schema)
