# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the CG depth tables + zone allowed_tags (CivicCast 3.0 — S18 gap 6, step 7).

Revision ID: 0045_cg_depth
Revises: 0044_cg_board_designer
Create Date: 2026-06-16

Closes the incumbent PEG graphics-depth parity (S6 "CG depth" addendum / S18 gap 6):

* ``bulletin_media`` — richer bulletin content (uploaded image / fullscreen
  slide / live-video input composited by the engine).
* ``bulletin_audio`` — per-bulletin narration or per-channel background bed,
  mixed under the S11 loudness path.
* ``zone_tags`` — channel-scoped tags; a zone's ``allowed_tags`` restricts it to
  tagged content.
* ``cg_zone_configs.allowed_tags`` — JSON list of tag ids (added column).

Revision numbering — repo-global single chain (ADR 0008). Parents on the real
head ``0044_cg_board_designer`` (S6 board designer). S6's addendum assigned
``0053``, but that number was never built — this takes the next monotonic number
after the real head. No foreign keys: ``bulletin_id`` / ``target_id`` /
``channel_id`` are soft string references resolved in the store.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_cg_depth"
down_revision = "0044_cg_board_designer"
branch_labels = None
depends_on = None

_MEDIA_KINDS = "'uploaded_image', 'fullscreen_slide', 'live_video'"
_AUDIO_SCOPES = "'bulletin', 'channel'"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        "bulletin_media",
        sa.Column("media_id", sa.String(length=120), nullable=False),
        sa.Column("bulletin_id", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("asset_ref", sa.String(length=120), nullable=True),
        sa.Column("live_source", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("media_id", name="bulletin_media_pkey"),
        sa.CheckConstraint(f"kind IN ({_MEDIA_KINDS})", name="bulletin_media_kind_check"),
        schema=schema,
    )
    op.create_index("ix_bulletin_media_bulletin", "bulletin_media", ["bulletin_id"], schema=schema)

    op.create_table(
        "bulletin_audio",
        sa.Column("audio_id", sa.String(length=120), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=120), nullable=False),
        sa.Column("asset_ref", sa.String(length=120), nullable=False),
        sa.Column(
            "loudness_regime", sa.String(length=40), nullable=False, server_default="inherit"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("audio_id", name="bulletin_audio_pkey"),
        sa.CheckConstraint(f"scope IN ({_AUDIO_SCOPES})", name="bulletin_audio_scope_check"),
        schema=schema,
    )
    op.create_index("ix_bulletin_audio_target", "bulletin_audio", ["target_id"], schema=schema)

    op.create_table(
        "zone_tags",
        sa.Column("tag_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("tag_id", name="zone_tags_pkey"),
        schema=schema,
    )
    op.create_index("ix_zone_tags_channel", "zone_tags", ["channel_id"], schema=schema)

    op.add_column(
        "cg_zone_configs",
        sa.Column("allowed_tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("cg_zone_configs", "allowed_tags", schema=schema)
    op.drop_index("ix_zone_tags_channel", table_name="zone_tags", schema=schema)
    op.drop_table("zone_tags", schema=schema)
    op.drop_index("ix_bulletin_audio_target", table_name="bulletin_audio", schema=schema)
    op.drop_table("bulletin_audio", schema=schema)
    op.drop_index("ix_bulletin_media_bulletin", table_name="bulletin_media", schema=schema)
    op.drop_table("bulletin_media", schema=schema)
