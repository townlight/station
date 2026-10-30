# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the channel program log (recurring slots + materialized occurrences).

Revision ID: 0031_program_log
Revises: 0030_webhook_retry_queue
Create Date: 2026-06-11

Cable automation CA-1: operator-defined recurring program slots per channel
materialize into real premiere ``schedule_items`` over a rolling horizon.
The UNIQUE (slot_id, occurrence_start) key on the occurrence table is the
materializer's idempotency guarantee; skipped occurrences are recorded with
an honest reason instead of silently retried.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031_program_log"
down_revision = "0030_webhook_retry_queue"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "channel_program_slots",
        sa.Column("slot_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("title_override", sa.String(length=200), nullable=True),
        sa.Column("recurrence", sa.String(length=16), nullable=False),
        sa.Column("first_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("repeat_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.PrimaryKeyConstraint("slot_id", name="channel_program_slots_pkey"),
        sa.CheckConstraint(
            "recurrence IN ('once', 'daily', 'weekly', 'weekdays')",
            name="channel_program_slots_recurrence_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_channel_program_slots_channel",
        "channel_program_slots",
        ["channel_id"],
        schema=schema,
    )
    op.create_table(
        "program_slot_occurrences",
        sa.Column("occurrence_id", sa.String(length=120), nullable=False),
        sa.Column("slot_id", sa.String(length=120), nullable=False),
        sa.Column("occurrence_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_item_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="scheduled"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("occurrence_id", name="program_slot_occurrences_pkey"),
        sa.UniqueConstraint(
            "slot_id",
            "occurrence_start",
            name="program_slot_occurrences_slot_start_key",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'skipped_conflict', 'skipped_asset', 'cancelled')",
            name="program_slot_occurrences_status_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_program_slot_occurrences_slot",
        "program_slot_occurrences",
        ["slot_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_program_slot_occurrences_slot",
        table_name="program_slot_occurrences",
        schema=schema,
    )
    op.drop_table("program_slot_occurrences", schema=schema)
    op.drop_index(
        "ix_channel_program_slots_channel",
        table_name="channel_program_slots",
        schema=schema,
    )
    op.drop_table("channel_program_slots", schema=schema)
