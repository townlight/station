# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Repair rc16 recording assets registered with a raw ``file://`` path.

Revision ID: 0072_normalize_recording_file_uris
Revises: 0071_published_blocks_overlap
Create Date: 2026-07-18

D3 (rc17 beta blockers): ``civiccast.live.finalization.LiveRecordingFinalizer``
used to persist ``assets.file_path`` as the raw ``recording_uri`` it was
called with -- for rehearsal and real live-session recordings alike, that
argument is a ``file://`` URI (``Path.as_uri()``), never a plain local path.
Every downstream reader of ``file_path`` treats it as a filesystem path
(``civiccast.schedule.media_integrity_worker`` calls ``Path(file_path)
.is_file()``, the staff package route calls ``Path(file_path).resolve()``)
so a ``file://`` string always resolves to a nonexistent path -- the
recording is on disk, but every rc16 install and every rehearsal reports it
``missing``. The application-level fix normalizes at registration time
(``civiccast/live/finalization.py``); this migration repairs rows an rc16
database already wrote before that fix existed.

Repairs only rows shaped like the defect (``file_path LIKE 'file://%'``):
rewrites ``file_path`` to the plain local path the URI encodes, and -- only
when that path resolves to a real file on THIS host -- also clears a stale
``missing`` flag by setting ``file_status='ok'``. A row whose backing file
genuinely is not present (moved/deleted media) is left with its file_path
corrected but its status untouched; the existing media-integrity scan
worker re-evaluates it on its own next pass, which is the single writer of
that column outside a relink and must stay the source of truth for it.

Downgrade is a deliberate, tested no-op (matching this repo's own
convention for irreversible data repairs, e.g. migration
``0070_grandfather_scheduled_to_published``): a plain local path
persisted post-upgrade is indistinguishable from a plain local path that
was *always* stored that way (every row written after the application-level
fix in ``civiccast/live/finalization.py``, and every non-rehearsal upload
row, already looks exactly like a repaired row). Blindly re-wrapping every
non-``file://`` ``file_path`` back into a URI on downgrade would corrupt
that unrelated, already-healthy data -- a real regression, not a rollback.
There is no marker column recording which rows this migration touched (and
adding one is a schema change outside this fix's charter). The safe,
correct downgrade is therefore to leave the repaired data in place; see
``tests/schedule/test_migration_0072.py`` for the round-trip proof that
downgrade is a genuine no-op (raises nothing, corrupts nothing).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit

import sqlalchemy as sa
from alembic import op

revision = "0072_normalize_recording_file_uris"
down_revision: str | None = "0071_published_blocks_overlap"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _local_path_from_file_uri(value: str) -> Path | None:
    """Resolve a ``file://`` URI to a local filesystem :class:`Path`, or None.

    Self-contained copy of ``civiccast.live.recording_paths
    .local_recording_path``'s ``file://`` handling (migrations in this repo
    do not import application code -- see the other files in this
    directory) including the Windows drive-letter fix: ``urlsplit``'s
    ``path`` component for ``file:///C:/recordings/x.mp4`` is
    ``/C:/recordings/x.mp4`` -- the leading slash has to be stripped before
    ``C:`` or the resulting path is invalid on Windows, which is this
    product's primary deployment target.
    """
    parsed = urlsplit(value)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    raw_path = unquote(parsed.path)
    if not raw_path.startswith("/"):
        return None
    if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return Path(raw_path)


def upgrade() -> None:
    bind = op.get_bind()
    assets = sa.table(
        "assets",
        sa.column("asset_id", sa.String()),
        sa.column("file_path", sa.Text()),
        sa.column("file_status", sa.String()),
        sa.column("file_status_checked_at", sa.DateTime(timezone=True)),
        schema=None if bind.dialect.name == "sqlite" else "civiccast",
    )
    rows = bind.execute(
        sa.select(assets.c.asset_id, assets.c.file_path).where(assets.c.file_path.like("file://%"))
    ).mappings()
    for row in rows:
        local_path = _local_path_from_file_uri(row["file_path"])
        if local_path is None:
            continue
        values: dict[str, object] = {"file_path": str(local_path)}
        if local_path.is_file():
            values["file_status"] = "ok"
            values["file_status_checked_at"] = sa.func.current_timestamp()
        bind.execute(sa.update(assets).where(assets.c.asset_id == row["asset_id"]).values(**values))


def downgrade() -> None:
    # Data repair is intentionally not reversed -- see the module docstring.
    # A repaired row's file_path is indistinguishable from a row that was
    # always stored as a plain local path, so there is nothing this
    # migration can safely rewrite back without risking healthy rows.
    return
