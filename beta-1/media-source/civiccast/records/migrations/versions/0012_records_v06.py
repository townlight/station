# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Create v0.6 signed record export tables.

Revision ID: 0012_records_v06
Revises: 0011_summary_v06
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_records_v06"
down_revision: str | None = "0011_summary_v06"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "record_exports",
        sa.Column("record_id", sa.String(length=160), primary_key=True),
        sa.Column("summary_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("audit_fingerprint", sa.String(length=136), nullable=False),
        sa.Column("artifact_digest", sa.String(length=71), nullable=False),
        sa.Column("pdfa_metadata_json", sa.Text(), nullable=False),
        sa.Column("timestamp_proof_json", sa.Text(), nullable=False),
        sa.Column("artifact_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('verified', 'failed')", name="record_exports_status_check"),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("record_exports", schema=_schema())
