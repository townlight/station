# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Track the trim each package was rendered with.

Revision ID: 0029_packaged_trim_bookkeeping
Revises: 0028_session_recording_target
Create Date: 2026-06-10

Beta sprint B3 (decision #4): operator trims must reach the published video.
``packaged_trim_in/out_seconds`` record what trim the on-disk HLS package was
rendered with; when the asset's trim diverges, the finalization worker
re-queues the job and re-renders (replacing the old manifest-exists-only
idempotency, which made trim edits decorative — audit ENG-004).

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_packaged_trim_bookkeeping"
down_revision = "0028_session_recording_target"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "live_finalization_jobs",
        sa.Column("packaged_trim_in_seconds", sa.Float(), nullable=True),
        schema=schema,
    )
    op.add_column(
        "live_finalization_jobs",
        sa.Column("packaged_trim_out_seconds", sa.Float(), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("live_finalization_jobs", schema=schema) as batch:
        batch.drop_column("packaged_trim_out_seconds")
        batch.drop_column("packaged_trim_in_seconds")
