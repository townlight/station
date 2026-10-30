# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add live recording finalization worker jobs.

Revision ID: 0023_live_finalization_jobs
Revises: 0022_egress_proof_source_ref
Create Date: 2026-06-09

History note: this migration originally shipped (unreleased, on
``work/audit-sprint-1`` only) as ``0011_live_finalization_jobs`` parented on
``0019_merge_v2_live_relay_heads``, which forked the alembic graph — the real
head was already ``0022_egress_proof_source_ref``. The revision was re-parented
and renumbered to 0023 before any release. Revision numbers are repo-global
(the chain spans every module's ``migrations/versions/`` directory), so always
run ``alembic heads`` and parent a new revision on the single current head.

Byte-size columns are ``BigInteger``: Postgres ``INTEGER`` caps at ~2 GiB, a
routine council-meeting recording size (SQLite cannot catch the width mistake —
its integers are 64-bit).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_live_finalization_jobs"
down_revision = "0022_egress_proof_source_ref"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "live_finalization_jobs",
        sa.Column("live_session_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("recording_uri", sa.Text(), nullable=True),
        sa.Column("recording_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("last_observed_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("asset_id", sa.String(length=64), nullable=True),
        sa.Column("local_package_manifest_path", sa.Text(), nullable=True),
        sa.Column("package_manifest_url", sa.Text(), nullable=True),
        sa.Column("trim_in_seconds", sa.Float(), nullable=True),
        sa.Column("trim_out_seconds", sa.Float(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            # CURRENT_TIMESTAMP is valid on both SQLite and Postgres and matches
            # the model's server_default exactly, so autogenerate sees no drift.
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("live_session_id", name="live_finalization_jobs_pkey"),
        sa.CheckConstraint(
            "state IN ('pending', 'running', 'failed', 'completed')",
            name="live_finalization_jobs_state_check",
        ),
        sa.CheckConstraint("attempts >= 0", name="live_finalization_jobs_attempts_nonneg"),
        sa.CheckConstraint(
            "max_attempts >= 1",
            name="live_finalization_jobs_max_attempts_positive",
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    schema_prefix = f"{schema}." if schema else ""
    if _use_schema():
        rows = op.get_bind().scalar(
            sa.text(f"SELECT count(*) FROM {schema_prefix}live_finalization_jobs")  # noqa: S608  # nosec B608
        )
        if rows and rows > 0:
            raise RuntimeError(
                "Refusing to downgrade past 0023_live_finalization_jobs: "
                f"{rows} finalization job row(s) exist. Delete them before downgrading."
            )
    op.drop_table("live_finalization_jobs", schema=schema)
