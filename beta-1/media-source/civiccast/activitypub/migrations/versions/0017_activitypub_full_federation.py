# SPDX-License-Identifier: Apache-2.0
"""v1.2 ActivityPub full federation persistence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_activitypub_full_federation"
down_revision = "0016_staff_tokens_v12"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "activitypub_followers",
        sa.Column("actor", sa.String(length=500), primary_key=True),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("activity_id", sa.String(length=500), nullable=False),
        sa.Column("inbox_url", sa.String(length=500), nullable=False),
        sa.Column("shared_inbox_url", sa.String(length=500), nullable=True),
        sa.Column("public_key_id", sa.String(length=500), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_activitypub_followers_domain",
        "activitypub_followers",
        ["domain"],
        schema=schema,
    )
    op.create_index(
        "ix_activitypub_followers_status",
        "activitypub_followers",
        ["status"],
        schema=schema,
    )
    op.create_table(
        "activitypub_outbox",
        sa.Column("activity_id", sa.String(length=500), primary_key=True),
        sa.Column("activity_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_table(
        "activitypub_delivery_attempts",
        sa.Column("delivery_id", sa.String(length=120), primary_key=True),
        sa.Column("activity_id", sa.String(length=500), nullable=False),
        sa.Column("inbox_url", sa.String(length=500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_activitypub_delivery_attempts_activity_id",
        "activitypub_delivery_attempts",
        ["activity_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index(
        "ix_activitypub_delivery_attempts_activity_id",
        table_name="activitypub_delivery_attempts",
        schema=schema,
    )
    op.drop_table("activitypub_delivery_attempts", schema=schema)
    op.drop_table("activitypub_outbox", schema=schema)
    op.drop_index(
        "ix_activitypub_followers_status",
        table_name="activitypub_followers",
        schema=schema,
    )
    op.drop_index(
        "ix_activitypub_followers_domain",
        table_name="activitypub_followers",
        schema=schema,
    )
    op.drop_table("activitypub_followers", schema=schema)
