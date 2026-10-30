# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add media-library hardening columns to ``assets`` (4.0 scope item 5).

Revision ID: 0062_media_integrity_columns
Revises: 0061_control_room_mode_gate
Create Date: 2026-07-05

Adds four nullable-safe columns needed for missing-file detection/relink,
thumbnails, and content-hash duplicate detection:

* ``content_hash`` — ``sha256:<hex>`` digest of the backing file, computed
  at ingest (see ``civiccast.schedule.ingest.hash_file``). Nullable: assets
  created before this migration, or via ``POST /api/staff/assets`` (which
  has no backing upload, only a manifest URL), have no file to hash.
* ``thumbnail_path`` — server-side path to the generated JPEG thumbnail,
  mirroring ``file_path``'s "server path, not a URL" convention. Nullable:
  generation can fail (corrupt file, ffmpeg absent) without blocking ingest.
* ``file_status`` — ``ok`` | ``missing`` | ``relinked``. Defaults to ``ok``
  so every pre-existing row is not retroactively flagged; the integrity
  scan (``civiccast.schedule.media_integrity_worker``) is the only writer
  of ``missing``, and the relink endpoint is the only writer of
  ``relinked``.
* ``file_status_checked_at`` — when the status was last set by a scan or
  relink. Nullable: never-scanned rows (most rows, until the worker's
  first pass) have no timestamp yet.

Following the repo-global single-chain convention (see migration 0043's
docstring): this schedule-module migration parents on the current real
head, ``0061_control_room_mode_gate`` (control_room module), not on any
number from a spec/plan document. ``0044`` was already taken by
``civiccast/cg/migrations/versions/0044_cg_board_designer.py`` — the
per-module directory numbering resets misled the first attempt at this
file; ``0062`` is the true next number in the single global chain.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062_media_integrity_columns"
down_revision = "0061_control_room_mode_gate"
branch_labels = None
depends_on = None

_FILE_STATUS_OK = "ok"
_FILE_STATUS_MISSING = "missing"
_FILE_STATUS_RELINKED = "relinked"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    # batch_alter_table: SQLite cannot ALTER a table to add a CHECK
    # constraint in place (see migration 0061's identical use of batch mode
    # for the same reason); Postgres runs the same calls as plain ALTERs.
    with op.batch_alter_table("assets", schema=schema) as batch:
        batch.add_column(sa.Column("content_hash", sa.String(length=71), nullable=True))
        batch.add_column(sa.Column("thumbnail_path", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column(
                "file_status",
                sa.String(length=16),
                nullable=False,
                server_default=_FILE_STATUS_OK,
            )
        )
        batch.add_column(
            sa.Column("file_status_checked_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(
            "assets_file_status_check",
            f"file_status IN ('{_FILE_STATUS_OK}', '{_FILE_STATUS_MISSING}', "
            f"'{_FILE_STATUS_RELINKED}')",
        )
    # Duplicate-detection lookups group by content_hash; non-unique because
    # NULL (unhashed) rows must coexist and legitimate duplicates are
    # expected to be reported, not rejected (spec: "non-destructive, report
    # never auto-delete"). Plain (non-batch) index creation works fine on
    # both dialects once the column exists.
    op.create_index(
        "assets_content_hash_idx",
        "assets",
        ["content_hash"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index("assets_content_hash_idx", table_name="assets", schema=schema)
    with op.batch_alter_table("assets", schema=schema) as batch:
        batch.drop_constraint("assets_file_status_check", type_="check")
        batch.drop_column("file_status_checked_at")
        batch.drop_column("file_status")
        batch.drop_column("thumbnail_path")
        batch.drop_column("content_hash")
