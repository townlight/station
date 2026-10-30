# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bind a board zone to a configured live source.

Revision ID: 0094_cg_live_video_zone
Revises: 0093_cg_board_audio_gain
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094_cg_live_video_zone"
down_revision = "0093_cg_board_audio_gain"
branch_labels = None
depends_on = None

_OLD_SOURCES = "'feed_adapter', 'manual', 'schedule', 'emergency', 'image', 'clock'"
_NEW_SOURCES = _OLD_SOURCES + ", 'live_video'"


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def _set_source_constraint(sources: str) -> None:
    with op.batch_alter_table("cg_zone_configs", schema=_schema()) as batch:
        batch.drop_constraint("cg_zone_configs_source_check", type_="check")
        batch.create_check_constraint(
            "cg_zone_configs_source_check", f"content_source IN ({sources})"
        )


def upgrade() -> None:
    op.add_column(
        "cg_zone_configs",
        sa.Column("live_source_id", sa.String(length=120), nullable=True),
        schema=_schema(),
    )
    _set_source_constraint(_NEW_SOURCES)


def downgrade() -> None:
    # Restore the old check only after changing new-source rows to an old,
    # truthful empty/manual state. Downgrade never silently leaves an invalid row.
    table = sa.Table(
        "cg_zone_configs",
        sa.MetaData(),
        sa.Column("content_source", sa.String()),
        schema=_schema(),
    )
    op.execute(
        table.update().where(table.c.content_source == "live_video").values(content_source="manual")
    )
    _set_source_constraint(_OLD_SOURCES)
    op.drop_column("cg_zone_configs", "live_source_id", schema=_schema())
