# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S7 watch-folder poll daemon -- config health/behavior columns + file ledger.

Revision ID: 0080_watch_folder_daemon
Revises: 0079_media_lifecycle
Create Date: 2026-08-25

PR #19 built the S7 ``WatchFolderConfig`` data model, CRUD API, and settings
UI but explicitly deferred the poll daemon itself -- nothing on disk polled
``monitor_path``, detected files, or called into ingest
(``docs/spec/3.0/sections/S7-media-lifecycle-and-readiness.md`` §6's
"watch-folder monitor (background daemon)" DONE criterion was unmet). This
migration is the schema half of that daemon build (see
``civiccast/schedule/watch_folder_worker.py`` for the daemon itself, and
``docs/adr/0024-watch-folder-daemon-processed-file-and-degraded-state.md``
for the design decisions these columns encode -- move-vs-leave-with-ledger
processed-file disposition, degraded/unreachable-path visibility, and the
delete-safety posture -- none of which the spec text itself resolves).

Adds to ``watch_folder_configs``:

* ``poll_interval_seconds``    -- how often the daemon scans this folder
  (spec §6 states a 5s default; NOT the same field as the pre-existing
  ``settle_window_seconds``, which is the write-completion stability
  window, D13)
* ``processed_file_mode``      -- ``leave_with_ledger`` (default; the file
  stays in ``monitor_path``, ingest state tracked in the new ledger table
  below) or ``move_to_subfolder`` (file moves to
  ``processed_subfolder_name`` under ``monitor_path`` after a successful
  ingest). Both modes NEVER delete the source file (delete-safety posture,
  ADR 0024).
* ``processed_subfolder_name`` -- subfolder name used only in
  ``move_to_subfolder`` mode
* ``health_status``            -- ``ok`` | ``degraded`` | ``unknown``
  (``unknown`` until the daemon's first poll of this config); an
  unreachable ``monitor_path`` (USB unplugged, SMB share down) sets
  ``degraded`` visibly on the config row rather than failing silently
* ``degraded_reason`` / ``degraded_since`` -- populated together, cleared
  together, the moment a poll of this folder succeeds again
* ``last_poll_at``             -- every poll attempt, success or failure
  (distinct from the pre-existing ``last_scanned_at``, which the daemon
  only advances on a poll that could actually list the directory)
* ``last_ingest_at``           -- the last time this config's daemon pass
  handed a file to the ingest pipeline successfully

Creates ``watch_folder_file_state`` -- the per-file settle-window and
reprocess-on-change ledger. One row per ``(config_id, file_path)``. This is
what makes the D13 "size+mtime stable across two consecutive polls" check
and "reprocess on change" semantics durable across daemon restarts, and
what the ``leave_with_ledger`` processed-file mode's "ledger" half refers
to.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0080_watch_folder_daemon"
down_revision: str | None = "0079_media_lifecycle"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _schema() -> str | None:
    return "civiccast" if _use_schema() else None


def upgrade() -> None:
    schema = _schema()

    op.add_column(
        "watch_folder_configs",
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="5"),
        schema=schema,
    )
    op.add_column(
        "watch_folder_configs",
        sa.Column(
            "processed_file_mode",
            sa.String(length=20),
            nullable=False,
            server_default="leave_with_ledger",
        ),
        schema=schema,
    )
    op.add_column(
        "watch_folder_configs",
        sa.Column(
            "processed_subfolder_name",
            sa.String(length=200),
            nullable=False,
            server_default="processed",
        ),
        schema=schema,
    )
    op.add_column(
        "watch_folder_configs",
        sa.Column("health_status", sa.String(length=16), nullable=False, server_default="unknown"),
        schema=schema,
    )
    op.add_column(
        "watch_folder_configs",
        sa.Column("degraded_reason", sa.Text(), nullable=True),
        schema=schema,
    )
    op.add_column(
        "watch_folder_configs",
        sa.Column("degraded_since", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.add_column(
        "watch_folder_configs",
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.add_column(
        "watch_folder_configs",
        sa.Column("last_ingest_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    if _use_schema():
        # SQLite: no support for ALTER-adding a CHECK constraint to an
        # existing table (see civiccast/schedule/migrations/versions/
        # 0006_widen_asset_state_check.py for the same posture). SQLite
        # test paths use Base.metadata.create_all from the ORM model, which
        # already carries these CHECKs via __table_args__, so nothing is
        # actually unenforced there -- only real ALTER-based upgrades on an
        # existing Postgres database need this block.
        op.create_check_constraint(
            "watch_folder_configs_poll_interval_check",
            "watch_folder_configs",
            "poll_interval_seconds >= 1",
            schema=schema,
        )
        op.create_check_constraint(
            "watch_folder_configs_processed_mode_check",
            "watch_folder_configs",
            "processed_file_mode IN ('leave_with_ledger', 'move_to_subfolder')",
            schema=schema,
        )
        op.create_check_constraint(
            "watch_folder_configs_health_status_check",
            "watch_folder_configs",
            "health_status IN ('ok', 'degraded', 'unknown')",
            schema=schema,
        )

    op.create_table(
        "watch_folder_file_state",
        sa.Column("state_id", sa.String(length=64), nullable=False),
        sa.Column("config_id", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_mtime", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("stable_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asset_id", sa.String(length=64), nullable=True),
        sa.Column("last_ingest_job_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=71), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state_id", name="watch_folder_file_state_pkey"),
        sa.CheckConstraint(
            "status IN ('pending', 'stable', 'ingesting', 'ingested', 'failed')",
            name="watch_folder_file_state_status_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_watch_folder_file_state_config_id",
        "watch_folder_file_state",
        ["config_id"],
        schema=schema,
    )
    op.create_index(
        "ux_watch_folder_file_state_config_path",
        "watch_folder_file_state",
        ["config_id", "file_path"],
        unique=True,
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index(
        "ux_watch_folder_file_state_config_path",
        table_name="watch_folder_file_state",
        schema=schema,
    )
    op.drop_index(
        "ix_watch_folder_file_state_config_id",
        table_name="watch_folder_file_state",
        schema=schema,
    )
    op.drop_table("watch_folder_file_state", schema=schema)

    if _use_schema():
        op.drop_constraint(
            "watch_folder_configs_health_status_check",
            "watch_folder_configs",
            type_="check",
            schema=schema,
        )
        op.drop_constraint(
            "watch_folder_configs_processed_mode_check",
            "watch_folder_configs",
            type_="check",
            schema=schema,
        )
        op.drop_constraint(
            "watch_folder_configs_poll_interval_check",
            "watch_folder_configs",
            type_="check",
            schema=schema,
        )
    op.drop_column("watch_folder_configs", "last_ingest_at", schema=schema)
    op.drop_column("watch_folder_configs", "last_poll_at", schema=schema)
    op.drop_column("watch_folder_configs", "degraded_since", schema=schema)
    op.drop_column("watch_folder_configs", "degraded_reason", schema=schema)
    op.drop_column("watch_folder_configs", "health_status", schema=schema)
    op.drop_column("watch_folder_configs", "processed_subfolder_name", schema=schema)
    op.drop_column("watch_folder_configs", "processed_file_mode", schema=schema)
    op.drop_column("watch_folder_configs", "poll_interval_seconds", schema=schema)
