# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add rollback audit fields to commit_to_air_reports (3.0 — S4 slice 5).

Revision ID: 0041_commit_rollback_fields
Revises: 0040_commit_to_air_reports
Create Date: 2026-06-15

The rollback endpoint records *why* an operator undid an airing and *when*.
``rollback_reason`` (operator's stated reason) and ``rolled_back_at`` are
nullable — set only when ``dispatch_status`` transitions to ``cancelled``.
Distinct from the commit-time ``operator_notes`` so the undo's audit is not
conflated with the original approval note.

Repo-global single chain — parents on the real head ``0040_commit_to_air_reports``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_commit_rollback_fields"
down_revision = "0040_commit_to_air_reports"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "commit_to_air_reports",
        sa.Column("rollback_reason", sa.Text(), nullable=True),
        schema=schema,
    )
    op.add_column(
        "commit_to_air_reports",
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("commit_to_air_reports", "rolled_back_at", schema=schema)
    op.drop_column("commit_to_air_reports", "rollback_reason", schema=schema)
