# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add durable caption review items.

Revision ID: 0025_caption_review_items
Revises: 0024_finalization_failure_codes
Create Date: 2026-06-09

Stage E of the audit sprint: caption review decisions are operator work
product on the public-record path and previously lived only in the in-memory
store, vanishing on restart even with durable storage active.

Revision numbers are repo-global — this module's first migration parents on
the current single head, which lives in ``civiccast/live/migrations/``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_caption_review_items"
down_revision = "0024_finalization_failure_codes"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "caption_review_items",
        sa.Column("review_item_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("cue_id", sa.String(length=160), nullable=False),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("low_confidence", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("reviewed_text", sa.Text(), nullable=True),
        sa.Column("reviewer_note", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("review_item_id", name="caption_review_items_pkey"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'edited', 'rejected')",
            name="caption_review_items_status_check",
        ),
        sa.CheckConstraint(
            "end_seconds > start_seconds",
            name="caption_review_items_cue_window_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_caption_review_items_asset_id",
        "caption_review_items",
        ["asset_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_caption_review_items_asset_id",
        table_name="caption_review_items",
        schema=schema,
    )
    op.drop_table("caption_review_items", schema=schema)
