# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the offline caption job queue (CivicCast One keystone K3).

Revision ID: 0075_offline_caption_jobs
Revises: 0074_caption_review_audio_evidence
Create Date: 2026-08-16

Captioning a published recording spans a model pass over the whole meeting
and then an operator's review of the result -- easily hours apart, across a
restart. The job therefore has to be durable, not an in-process task.

Revision numbers are repo-global (the chain spans every module's
``migrations/versions/`` directory), so this parents on the current single
head.

Audit finding 3 (MAJOR, caught before this revision shipped -- folded in
here rather than as a follow-on migration since ``0075`` is still
branch-only): ``enqueue_offline_caption_job`` was check-then-insert with no
DB-level guard, so two concurrent publish/retry calls for the same asset
could both pass the ``active_for_asset`` check and both insert a job, each
driving its own full ffmpeg+Whisper pass. Adds a partial-unique index on
``asset_id`` filtered to the two active states, mirroring the
``ix_control_room_sessions_one_open_per_surface`` pattern from
``0069_control_room_session_surface_lock``.
``PostgresOfflineCaptionJobStore.enqueue`` now catches the resulting
``IntegrityError`` and raises a clean ``OfflineCaptionJobConflictError``;
``enqueue_offline_caption_job`` catches that and returns the winning job
instead of a raw DB error.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075_offline_caption_jobs"
down_revision = "0074_caption_review_audio_evidence"
branch_labels = None
depends_on = None

_TABLE = "offline_caption_jobs"
_ACTIVE_INDEX = "ix_offline_caption_jobs_one_active_per_asset"
_ACTIVE_STATES_WHERE = "state IN ('pending', 'awaiting_review')"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        _TABLE,
        sa.Column("job_id", sa.String(length=120), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("package_dir", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published_cue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("job_id", name="offline_caption_jobs_pkey"),
        sa.CheckConstraint(
            "state IN ('pending', 'awaiting_review', 'complete', 'failed')",
            name="offline_caption_jobs_state_check",
        ),
        sa.CheckConstraint("attempts >= 0", name="offline_caption_jobs_attempts_check"),
        sa.CheckConstraint(
            "cue_count >= 0 AND published_cue_count >= 0",
            name="offline_caption_jobs_cue_count_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_offline_caption_jobs_asset_id",
        _TABLE,
        ["asset_id"],
        schema=schema,
    )
    # Partial-unique: at most one row per asset_id may sit in an active
    # state at a time. Postgres (the durable K3 path) and SQLite (the
    # test/managed path) both support partial indexes, so this is a real
    # DB-level guard on both, not just documentation.
    op.create_index(
        _ACTIVE_INDEX,
        _TABLE,
        ["asset_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text(_ACTIVE_STATES_WHERE),
        postgresql_where=sa.text(_ACTIVE_STATES_WHERE),
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(_ACTIVE_INDEX, table_name=_TABLE, schema=schema)
    op.drop_index(
        "ix_offline_caption_jobs_asset_id",
        table_name=_TABLE,
        schema=schema,
    )
    op.drop_table(_TABLE, schema=schema)
