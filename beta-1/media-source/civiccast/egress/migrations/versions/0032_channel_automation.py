# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the channel-automation auto_start intent to egress configs.

Revision ID: 0032_channel_automation
Revises: 0031_program_log
Create Date: 2026-06-11

Cable automation CA-2: ``auto_start`` is the durable "this channel runs
24/7" operator intent. The lifespan automation driver re-issues a start
command for flagged channels after app or machine restarts — the consumed
start command alone cannot survive a reboot.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032_channel_automation"
down_revision = "0031_program_log"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "egress_configs",
        sa.Column(
            "auto_start",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("egress_configs", "auto_start", schema=schema)
