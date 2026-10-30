# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add mid-recording source-dropout tracking to recording_jobs.

Revision ID: 0065_recording_dropout_fields
Revises: 0064_control_room_health_and_versioning
Create Date: 2026-07-05

Item 6 (Recording And Ingest Hardening): the capture pipeline now detects a
mid-recording source dropout, attempts reconnect, and records the event on
the job itself. ``dropout_count`` and ``last_dropout_at`` make each dropout
observable in the existing job list / support bundle without a new table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0065_recording_dropout_fields"
down_revision = "0064_control_room_health_and_versioning"
branch_labels = None
depends_on = None

_JOBS_TABLE = "recording_jobs"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.add_column(
        _JOBS_TABLE,
        sa.Column(
            "dropout_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=schema,
    )
    op.add_column(
        _JOBS_TABLE,
        sa.Column("last_dropout_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_column(_JOBS_TABLE, "last_dropout_at", schema=schema)
    op.drop_column(_JOBS_TABLE, "dropout_count", schema=schema)
