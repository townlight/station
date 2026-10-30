# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add durable S14 viewership store (events + rollups + report snapshots).

Revision ID: 0076_analytics_viewership
Revises: 0075_offline_caption_jobs
Create Date: 2026-08-21

S14 (Analytics / Audience Measurement) promotes the existing privacy-safe
beacon -> store -> report chain from a single JSON file
(``analytics-events.json``, ``AnalyticsStore`` in ``civiccast/analytics/
store.py``) to a durable, migrated Postgres-backed store. This is the first
migration for the ``civiccast/analytics`` module.

Creates three tables in one revision (per S14 spec §3.2 — "creates all
three S14 tables ... in one revision"):

* ``viewership_events`` — durable, privacy-filtered event rows (mirrors
  ``AnalyticsRetainedEvent`` + a derived ``stream_type`` column + the
  remaining safe-property allowlist as ``properties_json``, so the existing
  geography/device/platform/caption/audio/subscription/podcast dimension
  breakdowns keep working once the JSON store is retired).
* ``viewership_rollups`` — pre-aggregated VOD-24h / Live-30-min (or hourly
  for a single-day window) buckets the dashboard reads instead of scanning
  raw events on every request. ``UNIQUE(stream_type, bucket_kind,
  subject_id, bucket_start)`` makes the rollup worker's upsert idempotent.
* ``analytics_report_snapshots`` — a stored, dated report (drives
  year-over-year comparison and reproducible CSV/PDF exports; written by
  ``POST /api/staff/analytics/reports/board-pdf``).

Revision numbers are repo-global (the chain spans every module's
``migrations/versions/`` directory); this is the first ``civiccast/
analytics`` migration and parents on the current single head,
``0075_offline_caption_jobs``.

No JSON-file backfill runs inside this migration. Alembic migrations must
stay environment-agnostic and re-runnable across every deployment shape
(fresh install, restore, CI, a differently-configured managed-storage path)
— reaching into ``default_storage_dir()`` / ``analytics-events.json`` from a
DDL migration would tie schema evolution to a specific install's filesystem
layout and cannot be tested the way every other migration in this repo is
(SQLite unit + real-Postgres testcontainers, no filesystem fixtures). The
one-time, idempotent backfill the spec describes instead runs from
application code the first time durable storage activates with an empty
``viewership_events`` table and a legacy JSON file present — see
``civiccast.analytics.pg_store.backfill_json_events`` and its call site in
``civiccast/app.py``'s ``_wire_durable_stores``. This is a deliberate,
documented deviation from the spec's literal placement, not an omission.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076_analytics_viewership"
down_revision = "0075_offline_caption_jobs"
branch_labels = None
depends_on = None

_EVENTS = "viewership_events"
_ROLLUPS = "viewership_rollups"
_SNAPSHOTS = "analytics_report_snapshots"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        _EVENTS,
        sa.Column("event_id", sa.String(length=160), nullable=False),
        sa.Column("event_name", sa.String(length=80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("app_target", sa.String(length=80), nullable=False),
        sa.Column("stream_type", sa.String(length=8), nullable=False),
        sa.Column("channel_id", sa.String(length=160), nullable=True),
        sa.Column("content_id", sa.String(length=160), nullable=True),
        sa.Column("view_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("concurrent_viewers", sa.Integer(), nullable=True),
        sa.Column("geo_bucket", sa.String(length=80), nullable=True),
        sa.Column("properties_json", sa.Text(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("event_id", name="viewership_events_pkey"),
        sa.CheckConstraint(
            "stream_type IN ('vod', 'live')", name="viewership_events_stream_type_check"
        ),
        sa.CheckConstraint("view_seconds >= 0", name="viewership_events_view_seconds_check"),
        schema=schema,
    )
    op.create_index("ix_viewership_events_occurred_at", _EVENTS, ["occurred_at"], schema=schema)
    op.create_index("ix_viewership_events_stream_type", _EVENTS, ["stream_type"], schema=schema)
    op.create_index("ix_viewership_events_channel_id", _EVENTS, ["channel_id"], schema=schema)
    op.create_index("ix_viewership_events_content_id", _EVENTS, ["content_id"], schema=schema)

    op.create_table(
        _ROLLUPS,
        sa.Column("rollup_id", sa.String(length=200), nullable=False),
        sa.Column("stream_type", sa.String(length=8), nullable=False),
        sa.Column("bucket_kind", sa.String(length=16), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject_id", sa.String(length=160), nullable=False),
        sa.Column("viewer_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("time_viewed_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("peak_concurrent", sa.Integer(), nullable=True),
        sa.Column("avg_concurrent", sa.Float(), nullable=True),
        sa.Column("samples", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("rollup_id", name="viewership_rollups_pkey"),
        sa.CheckConstraint(
            "stream_type IN ('vod', 'live')", name="viewership_rollups_stream_type_check"
        ),
        sa.CheckConstraint(
            "bucket_kind IN ('day', 'halfhour', 'hour')",
            name="viewership_rollups_bucket_kind_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_viewership_rollups_unique_bucket",
        _ROLLUPS,
        ["stream_type", "bucket_kind", "subject_id", "bucket_start"],
        unique=True,
        schema=schema,
    )
    op.create_index(
        "ix_viewership_rollups_lookup",
        _ROLLUPS,
        ["stream_type", "bucket_kind", "bucket_start"],
        schema=schema,
    )

    op.create_table(
        _SNAPSHOTS,
        sa.Column("snapshot_id", sa.String(length=120), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=160), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id", name="analytics_report_snapshots_pkey"),
        schema=schema,
    )
    op.create_index(
        "ix_analytics_report_snapshots_generated_at",
        _SNAPSHOTS,
        ["generated_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_analytics_report_snapshots_generated_at", table_name=_SNAPSHOTS, schema=schema
    )
    op.drop_table(_SNAPSHOTS, schema=schema)

    op.drop_index("ix_viewership_rollups_lookup", table_name=_ROLLUPS, schema=schema)
    op.drop_index("ix_viewership_rollups_unique_bucket", table_name=_ROLLUPS, schema=schema)
    op.drop_table(_ROLLUPS, schema=schema)

    op.drop_index("ix_viewership_events_content_id", table_name=_EVENTS, schema=schema)
    op.drop_index("ix_viewership_events_channel_id", table_name=_EVENTS, schema=schema)
    op.drop_index("ix_viewership_events_stream_type", table_name=_EVENTS, schema=schema)
    op.drop_index("ix_viewership_events_occurred_at", table_name=_EVENTS, schema=schema)
    op.drop_table(_EVENTS, schema=schema)
