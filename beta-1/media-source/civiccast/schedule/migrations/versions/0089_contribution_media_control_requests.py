# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Record unverified browser media-control requests for remote guests.

The pinned VDO.Ninja v30.2 director iframe accepts targeted controls but does
not return an action callback. Existing contribution lifecycle rows therefore
must not be reinterpreted as proof that guest media was muted, removed, or
disconnected.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089_contribution_media_control_requests"
down_revision = "0088_live_caption_health_alert_rules"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "remote_guest_sessions",
        sa.Column(
            "media_control_state",
            sa.String(length=30),
            nullable=False,
            server_default="unknown",
        ),
        schema=schema,
    )
    op.add_column(
        "remote_guest_sessions",
        sa.Column("media_control_action", sa.String(length=30), nullable=True),
        schema=schema,
    )
    op.add_column(
        "remote_guest_sessions",
        sa.Column("media_control_requested_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("remote_guest_sessions", "media_control_requested_at", schema=schema)
    op.drop_column("remote_guest_sessions", "media_control_action", schema=schema)
    op.drop_column("remote_guest_sessions", "media_control_state", schema=schema)
