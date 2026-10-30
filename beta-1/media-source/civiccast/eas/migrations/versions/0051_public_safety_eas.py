# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S11c public-safety (EAS) ingest + display tables.

Three tables for the net-new ``civiccast/eas/`` module: ``eas_cap_sources`` (configured
CAP feeds), ``eas_cap_alerts`` (normalized CAP 1.2 alerts, deduped on sender+identifier),
``eas_display_decisions`` (resolved on-channel display actions, each stamped
``eas_claim='not_eas'`` — CivicCast displays public-safety information; it is not an EAS
device). Next sequential migration on the single global Alembic chain (ADR 0008); lands
on ``0050_caption_proof_samples``. (The spec's planned combined ``0044_loudness_and_eas``
number is stale; S11 ships per-slice migrations: 0049 loudness, 0050 caption proofs,
0051 EAS, then 0052 secondary audio.)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_public_safety_eas"
down_revision = "0050_caption_proof_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        "eas_cap_sources",
        sa.Column("source_id", sa.String(length=120), primary_key=True),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("endpoint_url", sa.String(length=1000), nullable=True),
        sa.Column("geocode_filter", sa.JSON(), nullable=False),
        sa.Column("severity_floor", sa.String(length=16), nullable=False, server_default="severe"),
        sa.Column("poll_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("credential_ref", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('ipaws-cap', 'nws-cap', 'amber-cap', 'manual')",
            name="eas_cap_sources_kind_check",
        ),
        sa.CheckConstraint(
            "severity_floor IN ('unknown', 'minor', 'moderate', 'severe', 'extreme')",
            name="eas_cap_sources_severity_floor_check",
        ),
        schema=schema,
    )

    op.create_table(
        "eas_cap_alerts",
        sa.Column("alert_id", sa.String(length=160), primary_key=True),
        sa.Column("source_id", sa.String(length=120), nullable=False),
        sa.Column("sender", sa.String(length=255), nullable=False),
        sa.Column("identifier", sa.String(length=255), nullable=False),
        sa.Column("sent", sa.DateTime(timezone=True), nullable=False),
        sa.Column("msg_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("event", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("urgency", sa.String(length=40), nullable=False, server_default="unknown"),
        sa.Column("certainty", sa.String(length=40), nullable=False, server_default="unknown"),
        sa.Column("headline", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("areas", sa.JSON(), nullable=False),
        sa.Column("references", sa.Text(), nullable=True),
        sa.Column("effective", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onset", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("sender", "identifier", name="eas_cap_alerts_dedup_key"),
        sa.CheckConstraint(
            "msg_type IN ('alert', 'update', 'cancel', 'ack', 'error')",
            name="eas_cap_alerts_msg_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'cancelled')",
            name="eas_cap_alerts_status_check",
        ),
        sa.CheckConstraint(
            "severity IN ('unknown', 'minor', 'moderate', 'severe', 'extreme')",
            name="eas_cap_alerts_severity_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_eas_cap_alerts_status_expires",
        "eas_cap_alerts",
        ["status", "expires"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_eas_cap_alerts_source", "eas_cap_alerts", ["source_id"], unique=False, schema=schema
    )

    op.create_table(
        "eas_display_decisions",
        sa.Column("decision_id", sa.String(length=160), primary_key=True),
        sa.Column("alert_id", sa.String(length=160), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.String(length=120), nullable=False),
        sa.Column("auto_surfaced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("overlay_id", sa.String(length=120), nullable=True),
        sa.Column("eas_claim", sa.String(length=16), nullable=False, server_default="not_eas"),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("displayed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('crawl', 'overlay', 'forced_slate')",
            name="eas_display_decisions_mode_check",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'displayed', 'cleared', 'expired')",
            name="eas_display_decisions_state_check",
        ),
        sa.CheckConstraint("eas_claim = 'not_eas'", name="eas_display_decisions_not_eas_check"),
        schema=schema,
    )
    op.create_index(
        "ix_eas_display_decisions_channel_state",
        "eas_display_decisions",
        ["channel_id", "state"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_eas_display_decisions_alert",
        "eas_display_decisions",
        ["alert_id"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index(
        "ix_eas_display_decisions_alert", table_name="eas_display_decisions", schema=schema
    )
    op.drop_index(
        "ix_eas_display_decisions_channel_state", table_name="eas_display_decisions", schema=schema
    )
    op.drop_table("eas_display_decisions", schema=schema)
    op.drop_index("ix_eas_cap_alerts_source", table_name="eas_cap_alerts", schema=schema)
    op.drop_index("ix_eas_cap_alerts_status_expires", table_name="eas_cap_alerts", schema=schema)
    op.drop_table("eas_cap_alerts", schema=schema)
    op.drop_table("eas_cap_sources", schema=schema)
