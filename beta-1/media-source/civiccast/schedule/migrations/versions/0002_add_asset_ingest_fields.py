# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""add asset ingest fields

Revision ID: 0002_add_asset_ingest_fields
Revises: 0001_create_assets_table
Create Date: 2026-05-09

Sprint 0.3 asset upload + ffprobe ingest:

  1. ``manifest_url`` becomes nullable — uploaded assets have no HLS
     manifest until the packager runs (Sprint 0.4).
  2. New ffprobe-extracted columns (all nullable).
  3. ``state`` column with a CHECK constraint enforcing the asset state
     machine values.

Per ADR 0008: both ``upgrade`` and ``downgrade`` implemented and tested
(``tests/schedule/test_migration_reversibility.py`` + real-Postgres
coverage in ``tests/schedule/test_real_postgres.py``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_add_asset_ingest_fields"
down_revision: str | None = "0001_create_assets_table"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    """Return True when the active dialect supports schemas (Postgres)."""
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    """Add ffprobe ingest columns + state machine to civiccast.assets."""
    schema = "civiccast" if _use_schema() else None
    tbl = "assets"

    # 1. Drop the NOT NULL constraint on manifest_url (Postgres only).
    #    SQLite does not support ALTER COLUMN, and its NOT NULL was never
    #    enforced at the migration level — the column is already effectively
    #    nullable in SQLite test runs via create_all.
    if _use_schema():
        op.alter_column(tbl, "manifest_url", nullable=True, schema=schema)

    # 2. Add ffprobe metadata columns.
    op.add_column(tbl, sa.Column("file_path", sa.Text(), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("file_size_bytes", sa.BigInteger(), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("codec_video", sa.String(50), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("codec_audio", sa.String(50), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("width_px", sa.Integer(), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("height_px", sa.Integer(), nullable=True), schema=schema)
    op.add_column(tbl, sa.Column("bitrate_bps", sa.BigInteger(), nullable=True), schema=schema)
    op.add_column(
        tbl,
        sa.Column("format_name", sa.String(100), nullable=True),
        schema=schema,
    )

    # 3. State column with check constraint (default 'pending_ingest').
    #    Existing rows (all have manifest_url set) get 'validated' to keep
    #    the public list/get API behaving identically before and after the
    #    migration.
    op.add_column(
        tbl,
        sa.Column(
            "state",
            sa.String(20),
            nullable=False,
            server_default="validated",
        ),
        schema=schema,
    )

    # 4. Tighten server_default to 'pending_ingest' now that existing rows
    #    are set to 'validated'. New rows inserted without an explicit state
    #    will now default to 'pending_ingest'.
    if _use_schema():
        op.alter_column(
            tbl,
            "state",
            server_default="pending_ingest",
            schema=schema,
        )

    # 5. Add check constraint on state (Postgres only — SQLite does not
    #    support ALTER TABLE ADD CONSTRAINT; the SA model carries the
    #    CheckConstraint for create_all paths, so fast-test SQLite coverage
    #    is preserved there).
    if _use_schema():
        op.create_check_constraint(
            "assets_state_check",
            tbl,
            "state IN ('pending_ingest', 'ingesting', 'validated', 'rejected')",
            schema=schema,
        )


def downgrade() -> None:
    """Remove ffprobe ingest columns + restore manifest_url NOT NULL."""
    schema = "civiccast" if _use_schema() else None
    tbl = "assets"

    if _use_schema():
        op.drop_constraint("assets_state_check", tbl, type_="check", schema=schema)
    for col in (
        "state",
        "format_name",
        "bitrate_bps",
        "height_px",
        "width_px",
        "codec_audio",
        "codec_video",
        "file_size_bytes",
        "file_path",
    ):
        op.drop_column(tbl, col, schema=schema)

    if _use_schema():
        op.alter_column(tbl, "manifest_url", nullable=False, schema=schema)
