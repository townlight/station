# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""create live_relay_configs

Revision ID: 0010_live_relay_configs
Revises: 0009_live_sources_index
Create Date: 2026-05-31

v1.8.7 adds optional remote ingest configuration without replacing the
free local RTMP path. A station that does nothing continues to use local
self-hosted ingest. A station that needs outbound-only remote ingest can
add a cloud relay row; a station whose hardware is offline can add a
direct-syndication RTMP target.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_live_relay_configs"
down_revision: str | None = "0009_live_sources_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _server_default_now() -> sa.sql.elements.TextClause:
    if _use_schema():
        return sa.text("now()")
    return sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    """Create optional relay configuration table."""
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        "live_relay_configs",
        sa.Column("relay_config_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("endpoint_url", sa.Text(), nullable=False),
        sa.Column("return_playback_url", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=True),
        sa.Column("credentials_handle", sa.String(length=200), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "health_state",
            sa.String(length=32),
            nullable=False,
            server_default="not_configured",
        ),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_server_default_now(),
        ),
        sa.PrimaryKeyConstraint("relay_config_id", name="live_relay_configs_pkey"),
        sa.CheckConstraint(
            "mode IN ('local_rtmp', 'cloud_rtmp_relay', 'direct_syndication')",
            name="live_relay_configs_mode_check",
        ),
        sa.CheckConstraint(
            "health_state IN ('not_configured', 'ready', 'degraded', 'offline')",
            name="live_relay_configs_health_state_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_live_relay_configs_channel_id",
        "live_relay_configs",
        ["channel_id"],
        schema=schema,
    )


def downgrade() -> None:
    """Drop optional relay configuration table."""
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_live_relay_configs_channel_id",
        table_name="live_relay_configs",
        schema=schema,
    )
    op.drop_table("live_relay_configs", schema=schema)
