# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the asset disposition review queue.

Revision ID: 0027_asset_disposition_reviews
Revises: 0026_activitypub_retry_queue
Create Date: 2026-06-10

Stage F of the audit sprint: the retention enforcement worker flags assets
whose retention schedule has expired into this append-once queue for
records-clerk review. The worker never deletes anything — automatic purge is
an explicit pending product decision.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_asset_disposition_reviews"
down_revision = "0026_activitypub_retry_queue"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "asset_disposition_reviews",
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("retention_policy", sa.String(length=20), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending_review"),
        sa.Column(
            "flagged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("asset_id", name="asset_disposition_reviews_pkey"),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_table("asset_disposition_reviews", schema=schema)
