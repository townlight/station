# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add failure_code / failure_detail to live finalization jobs.

Revision ID: 0024_finalization_failure_codes
Revises: 0023_live_finalization_jobs
Create Date: 2026-06-09

Stage B+D audit status-contract pass (UX-002/UX-003/DOC-009):
``failure_code`` is the stable machine identifier consumers branch on,
``failure_reason`` becomes operator-facing copy, and raw exception text moves
to ``failure_detail``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_finalization_failure_codes"
down_revision = "0023_live_finalization_jobs"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "live_finalization_jobs",
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        schema=schema,
    )
    op.add_column(
        "live_finalization_jobs",
        sa.Column("failure_detail", sa.Text(), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("live_finalization_jobs", schema=schema) as batch:
        batch.drop_column("failure_detail")
        batch.drop_column("failure_code")
