# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add bounded level control for bulletin-board background audio.

Revision ID: 0093_cg_board_audio_gain
Revises: 0092_notification_delivery_outcomes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0093_cg_board_audio_gain"
down_revision = "0092_notification_delivery_outcomes"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    op.add_column(
        "bulletin_audio",
        sa.Column("gain_db", sa.Float(), nullable=False, server_default="0"),
        schema=_schema(),
    )


def downgrade() -> None:
    op.drop_column("bulletin_audio", "gain_db", schema=_schema())
