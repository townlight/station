# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add control-room session mode gate fields.

Revision ID: 0061_control_room_mode_gate
Revises: 0060_recording_paywall_merge
Create Date: 2026-06-30

3.1 LPM control-room proof adds explicit Test Mode vs On-Air Mode sessions.
The fields are session-scoped because the operator must open a deliberate
On-Air session with a safe-state cue before device commands can leave CivicCast.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_control_room_mode_gate"
down_revision = "0060_recording_paywall_merge"
branch_labels = None
depends_on = None

_SESSION_MODES = "'test', 'on_air'"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("control_room_sessions", schema=schema) as batch:
        batch.add_column(
            sa.Column("mode", sa.String(length=20), nullable=False, server_default="test")
        )
        batch.add_column(sa.Column("safe_state_cue_id", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("on_air_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "control_room_sessions_mode_check",
            f"mode IN ({_SESSION_MODES})",
        )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("control_room_sessions", schema=schema) as batch:
        batch.drop_constraint("control_room_sessions_mode_check", type_="check")
        batch.drop_column("on_air_expires_at")
        batch.drop_column("safe_state_cue_id")
        batch.drop_column("mode")
