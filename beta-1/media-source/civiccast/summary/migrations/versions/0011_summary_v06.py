# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Create v0.6 sourced summary tables.

Revision ID: 0011_summary_v06
Revises: 0010_fractional_asset_trim
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_summary_v06"
down_revision: str | None = "0010_fractional_asset_trim"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "summaries",
        sa.Column("summary_id", sa.String(length=160), primary_key=True),
        sa.Column("meeting_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("audit_fingerprint", sa.String(length=71), nullable=False),
        sa.Column("operator_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'refused')",
            name="summaries_status_check",
        ),
        schema=schema,
    )
    op.create_table(
        "sourced_claims",
        sa.Column("claim_id", sa.String(length=160), primary_key=True),
        sa.Column("summary_id", sa.String(length=160), nullable=False),
        sa.Column("claim_type", sa.String(length=32), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("transcript_ranges_json", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "claim_type IN ('quantitative', 'narrative')",
            name="sourced_claims_type_check",
        ),
        schema=schema,
    )
    op.create_table(
        "summary_approvals",
        sa.Column("summary_id", sa.String(length=160), primary_key=True),
        sa.Column("operator_id", sa.String(length=160), nullable=False),
        sa.Column("operator_display_name", sa.String(length=200), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approval_note", sa.Text(), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_table("summary_approvals", schema=schema)
    op.drop_table("sourced_claims", schema=schema)
    op.drop_table("summaries", schema=schema)
