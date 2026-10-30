# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8 alerting tables + §6.2 default rule seed.

Creates the six S8 tables on the single global migration chain:

  alert_rules              — operator-tunable condition → severity + channels
  alert_channels           — push destinations (email / SMS / webhook); no secrets
  alert_events             — one firing event per (condition, resource_ref) dedupe key
  alert_event_deliveries   — per-channel delivery-attempt proofs
  system_resource_samples  — host metrics (CPU/RAM/GPU/disk/clock/db/service)
  system_self_tests        — daily + weekly self-test records

Seeds the §6.2 default rules (one per AlertConditionKind, operator-tunable after
install). OD-9: ``disk-low``, ``clock-skew``, ``db-unreachable``, ``service-down``
are first-class condition kinds added here so the rule table is self-documenting.

Migration-numbering reality (ADR 0008): the spec's planned ``0042`` is superseded
by as-built sequencing — this is the next free number after ``0038_reliability_fields``
(the S9 reliability migration, built first per master §10 step 3).
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0039_alerting_and_sinkhealth"
down_revision = "0038_reliability_fields"
branch_labels = None
depends_on = None

# Sentinel timestamp used for seed-row ``updated_at``. Must be a real datetime:
# the column is ``timestamp with time zone`` and Postgres will not implicitly
# cast a varchar parameter to timestamptz in a bulk INSERT (SQLite tolerated it).
_SEED_AT = datetime(2026, 6, 15, tzinfo=UTC)
_SEED_BY = "system:migration-seed"

# Default rule definitions (condition → severity, re_alert_after_seconds).
# channel_ids start empty — operator configures actual channels post-install.
# One-shot conditions (server-crash, missing-media) use re_alert_after_seconds=0.
_DEFAULT_RULES = [
    # critical / page-now
    ("off-air", "critical", 3600, 900),
    ("encoder-death", "critical", 3600, 900),
    ("server-crash", "critical", 0, 900),  # one-shot per boot
    ("relay-blocked", "critical", 1800, 900),
    ("missing-media", "critical", 0, 900),  # one-shot per item
    ("db-unreachable", "critical", 1800, 900),
    ("service-down", "critical", 3600, 900),
    # warning / attention-this-shift
    ("commit-failure", "warning", 1800, 900),
    ("compliance-probe-fail", "warning", 3600, 900),
    ("schema-drift", "warning", 21600, 900),
    ("takeover-stuck-2h", "warning", 7200, 900),
    ("disk-low", "warning", 3600, 900),
    ("clock-skew", "warning", 3600, 900),
    # info / record-only
    ("ai-runtime-down", "info", 21600, 900),
]


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    # -- alert_rules ----------------------------------------------------------
    op.create_table(
        "alert_rules",
        sa.Column("rule_id", sa.String(120), primary_key=True),
        sa.Column("condition", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("channel_ids_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column(
            "dedupe_window_seconds", sa.Integer(), nullable=False, server_default=sa.text("900")
        ),
        sa.Column(
            "re_alert_after_seconds", sa.Integer(), nullable=False, server_default=sa.text("3600")
        ),
        sa.Column("scope_channel_id", sa.String(80), nullable=True),
        sa.Column(
            "notify_on_resolve", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(120), nullable=False),
        sa.CheckConstraint(
            "severity IN ('critical', 'warning', 'info')",
            name="alert_rules_severity_check",
        ),
        schema=schema,
    )

    # -- alert_channels -------------------------------------------------------
    op.create_table(
        "alert_channels",
        sa.Column("channel_id", sa.String(120), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("target_redacted", sa.String(200), nullable=False),
        sa.Column("credential_handle", sa.String(200), nullable=True),
        sa.Column("quiet_hours_start_utc", sa.String(5), nullable=True),
        sa.Column("quiet_hours_end_utc", sa.String(5), nullable=True),
        sa.Column("last_delivery_status", sa.String(16), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('email', 'sms', 'webhook')",
            name="alert_channels_kind_check",
        ),
        schema=schema,
    )

    # -- alert_events ---------------------------------------------------------
    op.create_table(
        "alert_events",
        sa.Column("event_id", sa.String(120), primary_key=True),
        sa.Column("rule_id", sa.String(120), nullable=False),
        sa.Column("condition", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("resource_ref", sa.String(200), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("source_section", sa.String(8), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(120), nullable=True),
        sa.CheckConstraint(
            "severity IN ('critical', 'warning', 'info')",
            name="alert_events_severity_check",
        ),
        sa.CheckConstraint(
            "state IN ('firing', 'resolved')",
            name="alert_events_state_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_alert_events_dedupe",
        "alert_events",
        ["condition", "resource_ref", "state"],
        unique=False,
        schema=schema,
    )

    # -- alert_event_deliveries -----------------------------------------------
    op.create_table(
        "alert_event_deliveries",
        sa.Column("delivery_id", sa.String(120), primary_key=True),
        sa.Column("event_id", sa.String(120), nullable=False),
        sa.Column("alert_channel_id", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("signature", sa.String(200), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('sent', 'failed', 'suppressed', 'dead_letter')",
            name="alert_event_deliveries_status_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_alert_event_deliveries_event_id",
        "alert_event_deliveries",
        ["event_id"],
        unique=False,
        schema=schema,
    )

    # -- system_resource_samples ----------------------------------------------
    op.create_table(
        "system_resource_samples",
        sa.Column("sample_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("ram_used_gb", sa.Float(), nullable=True),
        sa.Column("ram_total_gb", sa.Float(), nullable=True),
        sa.Column("gpu_percent", sa.Float(), nullable=True),
        sa.Column("gpu_vram_used_gb", sa.Float(), nullable=True),
        sa.Column("media_volume_free_gb", sa.Float(), nullable=True),
        sa.Column("backup_volume_free_gb", sa.Float(), nullable=True),
        sa.Column("db_reachable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "backup_volume_writable", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("service_running", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("clock_skew_seconds", sa.Float(), nullable=True),
        schema=schema,
    )
    op.create_index(
        "ix_system_resource_samples_sampled_at",
        "system_resource_samples",
        ["sampled_at"],
        unique=False,
        schema=schema,
    )

    # -- system_self_tests ----------------------------------------------------
    op.create_table(
        "system_self_tests",
        sa.Column("self_test_id", sa.String(120), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("checks_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("summary", sa.String(600), nullable=False),
        sa.Column("evidence_path", sa.String(500), nullable=True),
        sa.CheckConstraint("kind IN ('daily', 'weekly')", name="system_self_tests_kind_check"),
        sa.CheckConstraint(
            "status IN ('pass', 'warn', 'fail')", name="system_self_tests_status_check"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_system_self_tests_started_at",
        "system_self_tests",
        ["started_at"],
        unique=False,
        schema=schema,
    )

    # -- §6.2 default rule seed -----------------------------------------------
    rules_table = sa.table(
        "alert_rules",
        sa.column("rule_id", sa.String),
        sa.column("condition", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("severity", sa.String),
        sa.column("channel_ids_json", sa.Text),
        sa.column("dedupe_window_seconds", sa.Integer),
        sa.column("re_alert_after_seconds", sa.Integer),
        sa.column("scope_channel_id", sa.String),
        sa.column("notify_on_resolve", sa.Boolean),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("updated_by", sa.String),
        # MUST carry the schema: tables are created in the civiccast schema
        # (version_table_schema) but the connection search_path is NOT set to
        # it (alembic/env.py), so an unqualified seed INSERT lands in `public`
        # and fails on real Postgres. None on SQLite (schema-less) → no-op.
        schema=schema,
    )
    op.bulk_insert(
        rules_table,
        [
            {
                "rule_id": f"default:{condition}",
                "condition": condition,
                "enabled": True,
                "severity": severity,
                "channel_ids_json": "[]",
                "dedupe_window_seconds": dedupe_window,
                "re_alert_after_seconds": re_alert_after,
                "scope_channel_id": None,
                "notify_on_resolve": True,
                "updated_at": _SEED_AT,
                "updated_by": _SEED_BY,
            }
            for condition, severity, re_alert_after, dedupe_window in _DEFAULT_RULES
        ],
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index("ix_system_self_tests_started_at", table_name="system_self_tests", schema=schema)
    op.drop_table("system_self_tests", schema=schema)
    op.drop_index(
        "ix_system_resource_samples_sampled_at", table_name="system_resource_samples", schema=schema
    )
    op.drop_table("system_resource_samples", schema=schema)
    op.drop_index(
        "ix_alert_event_deliveries_event_id", table_name="alert_event_deliveries", schema=schema
    )
    op.drop_table("alert_event_deliveries", schema=schema)
    op.drop_index("ix_alert_events_dedupe", table_name="alert_events", schema=schema)
    op.drop_table("alert_events", schema=schema)
    op.drop_table("alert_channels", schema=schema)
    op.drop_table("alert_rules", schema=schema)
