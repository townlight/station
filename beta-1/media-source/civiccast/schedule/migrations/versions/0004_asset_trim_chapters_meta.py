# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""add asset trim/chapter/retention fields

Revision ID: 0004_asset_trim_chapters_meta
Revises: 0003_create_schedule_items_table
Create Date: 2026-05-10

Sprint 0.3 task 5 — trim/chapter editor + metadata edit + retention
placeholder per release plan §0.3.

Adds five columns to ``civiccast.assets``:

  - ``trim_in_seconds`` (Integer, nullable) — start of trimmed window.
  - ``trim_out_seconds`` (Integer, nullable) — end of trimmed window.
  - ``chapters_json`` (Text, nullable) — operator-supplied chapters as
    serialized JSON list of {t, name, sub} objects.
  - ``retention_policy`` (String, NOT NULL, default 'default') —
    Sprint 0.3 placeholder per release plan §0.3 ("retention
    placeholder"); Sprint 0.7 archive module enforces. CHECK constraint
    pins the value set.
  - ``retention_until`` (DateTime tz-aware, nullable) — operator-set
    explicit retention deadline; Sprint 0.7 enforces.

Adds three CHECK constraints (Postgres only — SQLite carries them via
the SA model's ``__table_args__`` for ``create_all`` paths):

  - ``assets_retention_policy_check`` — pins the four-value enum.
  - ``assets_trim_in_nonneg`` — trim_in must be >= 0 if set.
  - ``assets_trim_window_ordered`` — trim_in < trim_out when both set.

All trim/chapter operations are non-destructive: the original file is
never modified. Trim and chapters are metadata applied at packaging
time (Sprint 0.4 packager reads these columns).

Per ADR 0008: both ``upgrade`` and ``downgrade`` implemented and tested.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_asset_trim_chapters_meta"
down_revision: str | None = "0003_create_schedule_items_table"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    """Add trim/chapter/retention columns + CHECK constraints."""
    schema = "civiccast" if _use_schema() else None
    tbl = "assets"

    op.add_column(tbl, sa.Column("trim_in_seconds", sa.Integer(), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("trim_out_seconds", sa.Integer(), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("chapters_json", sa.Text(), nullable=True), schema=schema)
    op.add_column(
        tbl,
        sa.Column(
            "retention_policy",
            sa.String(16),
            nullable=False,
            server_default="default",
        ),
        schema=schema,
    )
    op.add_column(
        tbl,
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )

    # CHECK constraints — Postgres only. SQLite paths get them via
    # the SA model's __table_args__ at create_all time.
    if _use_schema():
        op.create_check_constraint(
            "assets_retention_policy_check",
            tbl,
            "retention_policy IN ('default', 'permanent', 'meeting', 'short')",
            schema=schema,
        )
        op.create_check_constraint(
            "assets_trim_in_nonneg",
            tbl,
            "trim_in_seconds IS NULL OR trim_in_seconds >= 0",
            schema=schema,
        )
        op.create_check_constraint(
            "assets_trim_window_ordered",
            tbl,
            "trim_in_seconds IS NULL OR trim_out_seconds IS NULL OR "
            "trim_in_seconds < trim_out_seconds",
            schema=schema,
        )


def downgrade() -> None:
    """Drop the columns + CHECK constraints added in upgrade()."""
    schema = "civiccast" if _use_schema() else None
    tbl = "assets"

    if _use_schema():
        op.drop_constraint("assets_trim_window_ordered", tbl, type_="check", schema=schema)
        op.drop_constraint("assets_trim_in_nonneg", tbl, type_="check", schema=schema)
        op.drop_constraint("assets_retention_policy_check", tbl, type_="check", schema=schema)

    for col in (
        "retention_until",
        "retention_policy",
        "chapters_json",
        "trim_out_seconds",
        "trim_in_seconds",
    ):
        op.drop_column(tbl, col, schema=schema)
