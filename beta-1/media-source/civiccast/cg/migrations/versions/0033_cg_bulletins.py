# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add durable community bulletins + the per-channel fill policy.

Revision ID: 0033_cg_bulletins
Revises: 0032_channel_automation
Create Date: 2026-06-11

Cable automation CA-3: community bulletin submissions persist (they were
contract-only mock data), and each egress channel chooses what fills gaps
between scheduled programs: the plain slate (default) or the rotating
approved-bulletin board.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_cg_bulletins"
down_revision = "0032_channel_automation"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "cg_bulletins",
        sa.Column("submission_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("organization", sa.String(length=160), nullable=False),
        sa.Column("submitter_label", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("target_zone_kind", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="submitted"),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("moderation_notes", sa.Text(), nullable=True),
        sa.Column("approved_by_operator", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("submission_id", name="cg_bulletins_pkey"),
        sa.CheckConstraint(
            "state IN ('submitted', 'needs_changes', 'accepted', 'declined', 'scheduled')",
            name="cg_bulletins_state_check",
        ),
        sa.CheckConstraint(
            "target_zone_kind IN ('primary', 'ticker', 'schedule')",
            name="cg_bulletins_zone_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_cg_bulletins_channel",
        "cg_bulletins",
        ["channel_id"],
        schema=schema,
    )
    op.add_column(
        "egress_configs",
        sa.Column(
            "fill_policy",
            sa.String(length=20),
            nullable=False,
            server_default="slate",
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("egress_configs", "fill_policy", schema=schema)
    op.drop_index("ix_cg_bulletins_channel", table_name="cg_bulletins", schema=schema)
    op.drop_table("cg_bulletins", schema=schema)
