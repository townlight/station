# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Create v0.7 publish run persistence.

Revision ID: 0013_publish_v07
Revises: 0012_records_v06
Create Date: 2026-05-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_publish_v07"
down_revision: str | None = "0012_records_v06"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "publish_runs",
        sa.Column("asset_id", sa.String(length=160), primary_key=True),
        sa.Column("operator_id", sa.String(length=160), nullable=False),
        sa.Column("operator_display_name", sa.String(length=240), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("surfaces_json", sa.Text(), nullable=False),
        sa.Column("audit_events_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )


def downgrade() -> None:
    op.drop_table("publish_runs", schema=_schema())
