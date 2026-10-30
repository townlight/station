# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the ActivityPub delivery retry queue.

Revision ID: 0026_activitypub_retry_queue
Revises: 0025_caption_review_items
Create Date: 2026-06-10

Stage F of the audit sprint: failed follower deliveries (network error or
HTTP >= 400) are queued durably and retried with bounded exponential backoff
by the ActivityPub retry worker, dead-lettering after max attempts.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_activitypub_retry_queue"
down_revision = "0025_caption_review_items"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "activitypub_delivery_retries",
        sa.Column("retry_id", sa.String(length=120), nullable=False),
        sa.Column("activity_id", sa.String(length=500), nullable=False),
        sa.Column("inbox_url", sa.String(length=500), nullable=False),
        sa.Column("activity_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
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
        sa.PrimaryKeyConstraint("retry_id", name="activitypub_delivery_retries_pkey"),
        sa.CheckConstraint(
            "state IN ('pending', 'delivered', 'dead_letter')",
            name="activitypub_delivery_retries_state_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_activitypub_delivery_retries_activity",
        "activitypub_delivery_retries",
        ["activity_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_activitypub_delivery_retries_activity",
        table_name="activitypub_delivery_retries",
        schema=schema,
    )
    op.drop_table("activitypub_delivery_retries", schema=schema)
