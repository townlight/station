# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the subscriber webhook delivery retry queue.

Revision ID: 0030_webhook_retry_queue
Revises: 0029_packaged_trim_bookkeeping
Create Date: 2026-06-11

Issue #111: failed real webhook deliveries (network error or HTTP >= 400) are
queued durably and retried with bounded exponential backoff by the webhook
retry worker, dead-lettering after max attempts. The queue stores only the
subscription id and the notification payload — the webhook URL and the
per-subscription secret stay sealed in the subscriptions table and are
reopened at send time, so this table adds no plaintext PII.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0030_webhook_retry_queue"
down_revision = "0029_packaged_trim_bookkeeping"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "subscription_webhook_retries",
        sa.Column("retry_id", sa.String(length=120), nullable=False),
        sa.Column("subscription_id", sa.String(length=160), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("retry_id", name="subscription_webhook_retries_pkey"),
        sa.CheckConstraint(
            "state IN ('pending', 'delivered', 'dead_letter')",
            name="subscription_webhook_retries_state_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_subscription_webhook_retries_subscription",
        "subscription_webhook_retries",
        ["subscription_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_subscription_webhook_retries_subscription",
        table_name="subscription_webhook_retries",
        schema=schema,
    )
    op.drop_table("subscription_webhook_retries", schema=schema)
