# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""create egress control-plane tables

Revision ID: 0020_egress_control_plane
Revises: 0019_merge_v2_live_relay_heads
Create Date: 2026-06-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_egress_control_plane"
down_revision = "0019_merge_v2_live_relay_heads"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _schema() -> str | None:
    return "civiccast" if _use_schema() else None


def _server_default_now() -> sa.sql.elements.TextClause:
    if _use_schema():
        return sa.text("now()")
    return sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    """Create durable egress config, command, state, and health tables."""
    schema = _schema()
    now_default = _server_default_now()

    op.create_table(
        "egress_configs",
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("slate_message", sa.Text(), nullable=False),
        sa.Column(
            "loudness_target_lufs",
            sa.Float(),
            nullable=False,
            server_default=sa.text("-16.0"),
        ),
        sa.Column(
            "loudness_tolerance_lufs",
            sa.Float(),
            nullable=False,
            server_default=sa.text("2.0"),
        ),
        sa.Column("canonical_profile_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=now_default
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("channel_id", name="egress_configs_pkey"),
        schema=schema,
    )

    op.create_table(
        "egress_sinks",
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("uri", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.String(length=160), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default=sa.text("2000")),
        sa.Column(
            "extra_output_args_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.PrimaryKeyConstraint("channel_id", "label", name="egress_sinks_pkey"),
        sa.CheckConstraint(
            "kind IN ('srt', 'rtmp', 'local-ts', 'file', 'sdi')",
            name="egress_sinks_kind_check",
        ),
        schema=schema,
    )

    op.create_table(
        "egress_commands",
        sa.Column("command_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_by", sa.String(length=120), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("command_id", name="egress_commands_pkey"),
        sa.CheckConstraint(
            "action IN ('start', 'stop', 'reload', 'drain')",
            name="egress_commands_action_check",
        ),
        schema=schema,
    )

    op.create_table(
        "egress_states",
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("current_source_label", sa.String(length=200), nullable=True),
        sa.Column("current_proof_event_id", sa.String(length=120), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("channel_id", name="egress_states_pkey"),
        sa.CheckConstraint(
            "state IN ('STOPPED', 'STARTING', 'ON_AIR', 'TRANSITIONING', "
            "'FALLBACK_SLATE', 'DRAINING', 'STOPPING', 'ERROR')",
            name="egress_states_state_check",
        ),
        schema=schema,
    )

    op.create_table(
        "egress_health_samples",
        sa.Column("sample_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("sink_connected_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("encoder_fps", sa.Float(), nullable=True),
        sa.Column("encoder_bitrate_kbps", sa.Float(), nullable=True),
        sa.Column("dropped_frames", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("seconds_on_air", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_loudness_lufs", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("sample_id", name="egress_health_samples_pkey"),
        sa.CheckConstraint(
            "state IN ('STOPPED', 'STARTING', 'ON_AIR', 'TRANSITIONING', "
            "'FALLBACK_SLATE', 'DRAINING', 'STOPPING', 'ERROR')",
            name="egress_health_samples_state_check",
        ),
        schema=schema,
    )

    op.create_table(
        "egress_proof_events",
        sa.Column("event_id", sa.String(length=120), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("source_label", sa.String(length=200), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("proof_boundary", sa.String(length=160), nullable=False),
        sa.Column("machine_summary", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="egress_proof_events_pkey"),
        sa.CheckConstraint(
            "state IN ('STOPPED', 'STARTING', 'ON_AIR', 'TRANSITIONING', "
            "'FALLBACK_SLATE', 'DRAINING', 'STOPPING', 'ERROR')",
            name="egress_proof_events_state_check",
        ),
        schema=schema,
    )


def downgrade() -> None:
    """Drop the egress control-plane tables."""
    schema = _schema()
    op.drop_table("egress_proof_events", schema=schema)
    op.drop_table("egress_health_samples", schema=schema)
    op.drop_table("egress_states", schema=schema)
    op.drop_table("egress_commands", schema=schema)
    op.drop_table("egress_sinks", schema=schema)
    op.drop_table("egress_configs", schema=schema)
