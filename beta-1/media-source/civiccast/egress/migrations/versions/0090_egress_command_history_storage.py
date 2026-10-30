# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Index consumed egress commands for bounded history pruning.

Revision ID: 0090_egress_command_history_storage
Revises: 0089_contribution_media_control_requests
Create Date: 2026-10-13
"""

from __future__ import annotations

from alembic import op

revision = "0090_egress_command_history_storage"
down_revision = "0089_contribution_media_control_requests"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return None if op.get_bind().dialect.name == "sqlite" else "civiccast"


def upgrade() -> None:
    op.create_index(
        "ix_egress_commands_channel_consumed_issued",
        "egress_commands",
        ["channel_id", "consumed_at", "issued_at", "command_id"],
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_egress_commands_channel_consumed_issued",
        table_name="egress_commands",
        schema=_schema(),
    )
