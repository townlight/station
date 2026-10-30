# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Widen asset trim columns to fractional seconds.

Revision ID: 0010_fractional_asset_trim
Revises: 0009_live_sources_index
Create Date: 2026-05-13

Sprint 0.4 Slice 4 changes ``assets.trim_in_seconds`` and
``assets.trim_out_seconds`` from integer seconds to ``Numeric(10, 3)`` so
frame-step trim edits survive the database/API/packager path without
rounding to whole seconds.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_fractional_asset_trim"
down_revision: str | None = "0009_live_sources_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    """Allow millisecond-resolution trim windows."""
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("assets", schema=schema) as batch_op:
        batch_op.alter_column(
            "trim_in_seconds",
            existing_type=sa.Integer(),
            type_=sa.Numeric(10, 3),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "trim_out_seconds",
            existing_type=sa.Integer(),
            type_=sa.Numeric(10, 3),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Return trim windows to integer seconds for downgrade symmetry."""
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("assets", schema=schema) as batch_op:
        batch_op.alter_column(
            "trim_in_seconds",
            existing_type=sa.Numeric(10, 3),
            type_=sa.Integer(),
            existing_nullable=True,
            postgresql_using="floor(trim_in_seconds)::integer",
        )
        batch_op.alter_column(
            "trim_out_seconds",
            existing_type=sa.Numeric(10, 3),
            type_=sa.Integer(),
            existing_nullable=True,
            postgresql_using="ceil(trim_out_seconds)::integer",
        )
