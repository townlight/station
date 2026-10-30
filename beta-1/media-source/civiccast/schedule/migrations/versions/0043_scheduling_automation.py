# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the query-driven auto-scheduling tables (CivicCast 3.0 — S18 slice 1).

Revision ID: 0043_scheduling_automation
Revises: 0042_takeover_audit_and_command_action
Create Date: 2026-06-15

Closes PEG automation coverage gaps 1 (query-driven auto-scheduling) and 4 (block /
daypart scheduling), specified in S18 §5. Three tables:

* ``saved_searches`` — a named, declarative asset query (the query is stored as
  a JSON document, mirroring ``assets.chapters_json``, so the query vocabulary
  can grow without a migration).
* ``schedule_blocks`` — a daypart window on a channel (time-of-day range on
  selected weekdays, optionally bounded by calendar dates). Gap 4.
* ``auto_schedule_rules`` — binds a saved search to a daypart block on a
  channel with a pick strategy, rolling window (14-60 days), and a
  repeat-prevention window. Gap 1's compile rule.

Revision numbering — repo-global single chain. S18 §6 of the spec assigned this
migration ``0049`` when it was written, but the real chain has only advanced to
``0042_takeover_audit_and_command_action`` (S5 takeover) — the intervening
spec numbers (0043-0048) were never built. This migration therefore takes the
next monotonic number after the *real* head (``0043``) and parents on it so the
chain stays linear and single-headed (``schema_check.expected_migration_head``
raises if more than one head exists). The parent lives in the egress module's
versions dir; cross-module parenting is the established pattern (``0040`` in the
schedule dir parents on ``0039`` in the alerting dir).

No foreign keys: ``auto_schedule_rules.saved_search_id`` /
``schedule_block_id`` and ``schedule_blocks.channel_id`` are soft string
references resolved in the store, matching the schedule module's existing
convention (``schedule_items.asset_id`` has no FK). Deleting a rule must not
cascade into the search/block it named, and a planning rule should outlive the
channel-config churn it points at.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_scheduling_automation"
down_revision = "0042_takeover_audit_and_command_action"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        "saved_searches",
        sa.Column("saved_search_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("query_json", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("saved_search_id", name="saved_searches_pkey"),
        schema=schema,
    )

    op.create_table(
        "schedule_blocks",
        sa.Column("block_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("start_minute", sa.Integer(), nullable=False),
        sa.Column("end_minute", sa.Integer(), nullable=False),
        sa.Column("days_of_week_json", sa.Text(), nullable=False),
        sa.Column("active_from", sa.Date(), nullable=True),
        sa.Column("active_until", sa.Date(), nullable=True),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
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
        sa.CheckConstraint(
            "start_minute >= 0 AND start_minute < 1440",
            name="schedule_blocks_start_minute_check",
        ),
        sa.CheckConstraint(
            "end_minute > 0 AND end_minute <= 1440",
            name="schedule_blocks_end_minute_check",
        ),
        sa.PrimaryKeyConstraint("block_id", name="schedule_blocks_pkey"),
        schema=schema,
    )
    op.create_index(
        "schedule_blocks_channel_enabled_idx",
        "schedule_blocks",
        ["channel_id", "enabled"],
        schema=schema,
    )

    op.create_table(
        "auto_schedule_rules",
        sa.Column("rule_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("saved_search_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("schedule_block_id", sa.String(length=64), nullable=False),
        sa.Column(
            "pick_strategy",
            sa.String(length=20),
            nullable=False,
            server_default="newest",
        ),
        sa.Column(
            "rolling_window_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
        sa.Column(
            "repeat_prevention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("100"),
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_materialized_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "pick_strategy IN ('top_result', 'random_result', 'newest')",
            name="auto_schedule_rules_pick_strategy_check",
        ),
        sa.CheckConstraint(
            "rolling_window_days BETWEEN 14 AND 60",
            name="auto_schedule_rules_rolling_window_check",
        ),
        sa.CheckConstraint(
            "repeat_prevention_days >= 0",
            name="auto_schedule_rules_repeat_prevention_check",
        ),
        sa.PrimaryKeyConstraint("rule_id", name="auto_schedule_rules_pkey"),
        schema=schema,
    )
    op.create_index(
        "auto_schedule_rules_channel_enabled_idx",
        "auto_schedule_rules",
        ["channel_id", "enabled", "priority"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "auto_schedule_rules_channel_enabled_idx",
        table_name="auto_schedule_rules",
        schema=schema,
    )
    op.drop_table("auto_schedule_rules", schema=schema)
    op.drop_index(
        "schedule_blocks_channel_enabled_idx",
        table_name="schedule_blocks",
        schema=schema,
    )
    op.drop_table("schedule_blocks", schema=schema)
    op.drop_table("saved_searches", schema=schema)
