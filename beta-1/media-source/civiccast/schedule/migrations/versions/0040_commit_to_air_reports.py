# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the commit-to-air audit reports table (CivicCast 3.0 — S4 slice 1).

Revision ID: 0040_commit_to_air_reports
Revises: 0039_alerting_and_sinkhealth
Create Date: 2026-06-15

The Commit-to-Air gate (S4) records every operator "air this" approval as an
append-and-update audit row. The row is created ``pending``, advanced to
``queued`` once dispatched to the egress automation layer, and to
``acknowledged`` / ``error`` / ``cancelled`` as the dispatch resolves or the
operator rolls back.

Revision numbering — repo-global single chain. S4 §3 of the spec was written
when ``0037_asset_meeting_body`` was head and called this migration ``0038``,
parented on ``0037``. Since then the S9 reliability work added
``0038_reliability_fields`` and the S8 alerting work added
``0039_alerting_and_sinkhealth`` (the current head). This migration therefore
takes the next monotonic number (``0040``) and parents on the *real* head so
the chain stays linear and single-headed (``schema_check.expected_migration_head``
raises if more than one head exists).

No foreign keys: the reference columns (``channel_id``, ``occurrence_id``,
``schedule_item_id``, ``asset_id``) are soft string references, matching the
schedule module's existing convention (``schedule_items.asset_id`` has no FK
either). ``schedule_item_id`` holds a ``schedule_items.id`` UUID value as text
— a VARCHAR FK to a UUID PK is a type mismatch Postgres rejects — and an audit
record must outlive cancellation/deletion of the item it refers to.

dispatch_status CHECK includes ``cancelled``: S4 §3 wrote the CHECK as the
four values pending/queued/acknowledged/error, but §4's rollback endpoint sets
``cancelled``. The four-value CHECK would reject the documented rollback write,
so the constraint here is the five-value superset.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_commit_to_air_reports"
down_revision = "0039_alerting_and_sinkhealth"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "commit_to_air_reports",
        sa.Column("report_id", sa.String(length=64), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("occurrence_id", sa.String(length=120), nullable=False),
        sa.Column("schedule_item_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("approved_by_operator_id", sa.String(length=80), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conflicts_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gaps_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "dispatch_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("dispatch_error_detail", sa.Text(), nullable=True),
        sa.Column("dispatch_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("operator_notes", sa.Text(), nullable=True),
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
            "dispatch_status IN ('pending', 'queued', 'acknowledged', 'error', 'cancelled')",
            name="commit_to_air_reports_dispatch_status_check",
        ),
        sa.PrimaryKeyConstraint("report_id", name="commit_to_air_reports_pkey"),
        schema=schema,
    )
    op.create_index(
        "commit_to_air_reports_channel_approved_idx",
        "commit_to_air_reports",
        ["channel_id", "approved_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "commit_to_air_reports_channel_approved_idx",
        table_name="commit_to_air_reports",
        schema=schema,
    )
    op.drop_table("commit_to_air_reports", schema=schema)
