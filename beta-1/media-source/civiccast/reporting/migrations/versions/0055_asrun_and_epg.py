# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S23 as-run / proof-of-performance + EPG export: as_run_log + epg_export_configs.

Two tables for the net-new ``civiccast/reporting/`` module:

* ``as_run_log`` — append-only as-aired ledger (what the playout engine ACTUALLY
  emitted, engine-verified), distinct from the scheduled intent. A CHECK pins
  ``source_kind`` to the five kinds (``program``/``filler``/``live``/``slate`` +
  ``spot`` reserved for S24 underwriting); indexes on ``(channel_id,
  actual_start)`` and ``(station_id, actual_start)`` drive the date-range/channel
  as-run + shows reports, and an ``asset_id`` index serves the hours-by-category
  join to ``custom_field_values.asset_id``.
* ``epg_export_configs`` — EPG/TV-guide export profiles; a CHECK pins ``format``
  to ``xlist``/``xmltv``/``csv``; ``field_map`` is a JSON column; an index on
  ``station_id`` drives the per-station config list.

Next sequential migration on the single global Alembic chain (ADR 0008); sequences
after ``0054_custom_metadata_fields`` (S22). (The spec's planned number ``0056`` is the
stale next-free placeholder — S22 shipped as 0054, so as-run is 0055.)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0055_asrun_and_epg"
down_revision = "0054_custom_metadata_fields"
branch_labels = None
depends_on = None

_ASRUN_TABLE = "as_run_log"
_EPG_TABLE = "epg_export_configs"


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        _ASRUN_TABLE,
        sa.Column("entry_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("schedule_item_id", sa.String(length=120), nullable=True),
        sa.Column("asset_id", sa.String(length=120), nullable=True),
        sa.Column("scheduled_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actual_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actual_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_s", sa.Integer(), nullable=False),
        sa.Column("source_kind", sa.String(length=16), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_kind IN ('program', 'filler', 'live', 'slate', 'spot')",
            name="as_run_log_source_kind_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_as_run_log_channel_actual_start",
        _ASRUN_TABLE,
        ["channel_id", "actual_start"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_as_run_log_station_actual_start",
        _ASRUN_TABLE,
        ["station_id", "actual_start"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_as_run_log_asset",
        _ASRUN_TABLE,
        ["asset_id"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        _EPG_TABLE,
        sa.Column("config_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("format", sa.String(length=16), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("endpoint", sa.String(length=500), nullable=True),
        sa.Column("field_map", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "format IN ('xlist', 'xmltv', 'csv')",
            name="epg_export_configs_format_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_epg_export_configs_station",
        _EPG_TABLE,
        ["station_id"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index("ix_epg_export_configs_station", table_name=_EPG_TABLE, schema=schema)
    op.drop_table(_EPG_TABLE, schema=schema)
    op.drop_index("ix_as_run_log_asset", table_name=_ASRUN_TABLE, schema=schema)
    op.drop_index("ix_as_run_log_station_actual_start", table_name=_ASRUN_TABLE, schema=schema)
    op.drop_index("ix_as_run_log_channel_actual_start", table_name=_ASRUN_TABLE, schema=schema)
    op.drop_table(_ASRUN_TABLE, schema=schema)
