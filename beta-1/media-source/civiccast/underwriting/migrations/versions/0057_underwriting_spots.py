# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S24 underwriting / sponsorship-spot management: underwriting_spots + spot_flights + spot_placements.

Three tables for the net-new ``civiccast/underwriting/`` module:

* ``underwriting_spots`` — sponsor identity + asset id + the operator's editorial
  47 CFR 73.503 attestation. Indexed on ``(station_id)``, ``(station_id,
  underwriter)`` for the per-underwriter affidavit rollup, and ``(asset_id)``
  for the asset → spot reverse lookup.
* ``spot_flights`` — flight window + frequency cap + optional S19 daypart-block
  id + channel scope. CHECKs enforce ``end_date >= start_date`` and
  ``frequency_cap_per_day`` range.
* ``spot_placements`` — what the trafficking compiler materialized. Indexed for
  the upcoming-and-aired-insertions-per-channel view and the as-run-affidavit
  walk back to flight/spot/underwriter.

Per-underwriter affidavits are NOT a table — they are a report view over S23's
``as_run_log`` joined through ``spot_placements`` (slice 3 ``service.py``).

This migration sequences after ``0055_asrun_and_epg`` (S23, shipped). The slot
``0056`` is reserved for S21 (scheduled-recording) per RECONCILIATION.md (D17
on underwriting scope + the post-D19 chain-shape footer, which is the canonical
0056-reservation cite) — when S21 lands, its migration will be a sibling on
top of ``0055`` and an Alembic merge revision will unify the two heads. This
non-collision approach is the spec-canonical path (S24 spec line 3 + line 63).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057_underwriting_spots"
down_revision = "0055_asrun_and_epg"
branch_labels = None
depends_on = None

_SPOTS_TABLE = "underwriting_spots"
_FLIGHTS_TABLE = "spot_flights"
_PLACEMENTS_TABLE = "spot_placements"
# E-5 follow-up: a composite index on as_run_log lets the affidavit hot path
# scan just the spot rows for a period instead of the full month, then
# discarding ~99% in Python. Created here (additively) because 0057 is the
# current uncommitted head and a fresh 0058 would collide with the S25
# reservation.
_AS_RUN_TABLE = "as_run_log"
_AS_RUN_SOURCE_KIND_INDEX = "ix_as_run_log_source_kind_actual_start"


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        _SPOTS_TABLE,
        sa.Column("spot_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("underwriter", sa.String(length=200), nullable=False),
        sa.Column("asset_id", sa.String(length=120), nullable=False),
        sa.Column(
            "fcc_compliant_ack", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_underwriting_spots_station",
        _SPOTS_TABLE,
        ["station_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_underwriting_spots_station_underwriter",
        _SPOTS_TABLE,
        ["station_id", "underwriter"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_underwriting_spots_asset",
        _SPOTS_TABLE,
        ["asset_id"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        _FLIGHTS_TABLE,
        sa.Column("flight_id", sa.String(length=120), primary_key=True),
        sa.Column("spot_id", sa.String(length=120), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("frequency_cap_per_day", sa.Integer(), nullable=True),
        sa.Column("daypart_block_id", sa.String(length=120), nullable=True),
        sa.Column("channels", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("end_date >= start_date", name="spot_flights_date_order_check"),
        sa.CheckConstraint(
            "frequency_cap_per_day IS NULL OR (frequency_cap_per_day >= 1 "
            "AND frequency_cap_per_day <= 1440)",
            name="spot_flights_freq_cap_range_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_spot_flights_spot",
        _FLIGHTS_TABLE,
        ["spot_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_spot_flights_window",
        _FLIGHTS_TABLE,
        ["start_date", "end_date"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        _PLACEMENTS_TABLE,
        sa.Column("placement_id", sa.String(length=120), primary_key=True),
        sa.Column("flight_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_item_id", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_spot_placements_channel_scheduled",
        _PLACEMENTS_TABLE,
        ["channel_id", "scheduled_at"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_spot_placements_flight",
        _PLACEMENTS_TABLE,
        ["flight_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_spot_placements_schedule_item",
        _PLACEMENTS_TABLE,
        ["schedule_item_id"],
        unique=False,
        schema=schema,
    )

    # E-5 follow-up: composite index for the affidavit billing-hot-path scan.
    op.create_index(
        _AS_RUN_SOURCE_KIND_INDEX,
        _AS_RUN_TABLE,
        ["source_kind", "actual_start"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index(_AS_RUN_SOURCE_KIND_INDEX, table_name=_AS_RUN_TABLE, schema=schema)
    op.drop_index("ix_spot_placements_schedule_item", table_name=_PLACEMENTS_TABLE, schema=schema)
    op.drop_index("ix_spot_placements_flight", table_name=_PLACEMENTS_TABLE, schema=schema)
    op.drop_index(
        "ix_spot_placements_channel_scheduled", table_name=_PLACEMENTS_TABLE, schema=schema
    )
    op.drop_table(_PLACEMENTS_TABLE, schema=schema)

    op.drop_index("ix_spot_flights_window", table_name=_FLIGHTS_TABLE, schema=schema)
    op.drop_index("ix_spot_flights_spot", table_name=_FLIGHTS_TABLE, schema=schema)
    op.drop_table(_FLIGHTS_TABLE, schema=schema)

    op.drop_index("ix_underwriting_spots_asset", table_name=_SPOTS_TABLE, schema=schema)
    op.drop_index(
        "ix_underwriting_spots_station_underwriter", table_name=_SPOTS_TABLE, schema=schema
    )
    op.drop_index("ix_underwriting_spots_station", table_name=_SPOTS_TABLE, schema=schema)
    op.drop_table(_SPOTS_TABLE, schema=schema)
