# SPDX-License-Identifier: Apache-2.0
"""v0.8 subscription persistence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_subscribe_v08"
down_revision = "0013_publish_v07"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "subscriptions",
        sa.Column("subscription_id", sa.String(length=160), primary_key=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("encrypted_subscriber_handle", sa.Text(), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmation_token", sa.Text(), nullable=False),
        sa.Column("unsubscribe_token", sa.Text(), nullable=False),
        sa.Column("encrypted_webhook_secret", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.create_index(
        "ix_subscriptions_target",
        "subscriptions",
        ["target_type", "target_id", "status"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_subscriptions_target", table_name="subscriptions", schema=schema)
    op.drop_table("subscriptions", schema=schema)
