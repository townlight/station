# SPDX-License-Identifier: Apache-2.0
"""v0.8 podcast persistence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_podcast_v08"
down_revision = "0014_subscribe_v08"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "podcast_episodes",
        sa.Column("episode_id", sa.String(length=160), primary_key=True),
        sa.Column("asset_id", sa.String(length=160), nullable=False, unique=True),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("audio_url", sa.Text(), nullable=False),
        sa.Column("rss_guid", sa.String(length=240), nullable=False, unique=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("loudness_lufs", sa.Float(), nullable=False),
        sa.Column("chapters_json", sa.Text(), nullable=False),
        sa.Column("transcript_url", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_podcast_episodes_channel",
        "podcast_episodes",
        ["channel_id", "published_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index("ix_podcast_episodes_channel", table_name="podcast_episodes", schema=schema)
    op.drop_table("podcast_episodes", schema=schema)
