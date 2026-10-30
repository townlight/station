# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Create the summary generation job queue table.

Field evidence (candidate #17, CPU-only 32GB reference station): the synchronous
``POST /api/staff/summaries/generate`` request 503'd at ~120s even when Ollama
itself succeeded, because a legitimate CPU-only summary generation (measured
94-366s+) cannot survive inside one HTTP request/response cycle. This table backs
the async summary generation job (``civiccast/summary/job.py``) -- the same durable
background-job pattern the offline caption job (``0075_offline_caption_jobs``, K3)
already established for "AI work that can legitimately take minutes."

Revision ID: 0081_summary_generation_jobs
Revises: 0080_watch_folder_daemon
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0081_summary_generation_jobs"
down_revision: str | None = "0080_watch_folder_daemon"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "summary_generation_jobs",
        sa.Column("job_id", sa.String(length=120), primary_key=True),
        sa.Column("meeting_id", sa.String(length=160), nullable=False),
        sa.Column("cues_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_id", sa.String(length=160), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'complete', 'failed')",
            name="summary_generation_jobs_state_check",
        ),
        sa.CheckConstraint("attempts >= 0", name="summary_generation_jobs_attempts_check"),
        schema=schema,
    )
    op.create_index(
        "ix_summary_generation_jobs_meeting_id",
        "summary_generation_jobs",
        ["meeting_id"],
        schema=schema,
    )
    # Partial-unique: at most one ACTIVE (pending/running) job per meeting -- the
    # DB-level guard against two concurrent enqueues both passing the app-level
    # check-then-insert (mirrors ix_offline_caption_jobs_one_active_per_asset,
    # 0075_offline_caption_jobs).
    op.create_index(
        "ix_summary_generation_jobs_one_active_per_meeting",
        "summary_generation_jobs",
        ["meeting_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('pending', 'running')"),
        sqlite_where=sa.text("state IN ('pending', 'running')"),
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index(
        "ix_summary_generation_jobs_one_active_per_meeting",
        table_name="summary_generation_jobs",
        schema=schema,
    )
    op.drop_index(
        "ix_summary_generation_jobs_meeting_id",
        table_name="summary_generation_jobs",
        schema=schema,
    )
    op.drop_table("summary_generation_jobs", schema=schema)
