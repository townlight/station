# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S11 gap 9 (SAP / descriptive audio): the ``audio_program_tracks`` table.

A show/channel can carry a primary audio program plus secondary tracks (a SAP
second-language program or descriptive/audio-description). The GStreamer engine muxes
each as an additional MPEG-TS audio PID; web/OTT exposes them as selectable renditions.
Next sequential migration on the single global Alembic chain (ADR 0008); lands on
``0051_public_safety_eas``. (S11 ships per-slice migrations: 0049 loudness, 0050 caption
proofs, 0051 EAS, 0052 secondary audio.)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_secondary_audio"
down_revision = "0051_public_safety_eas"
branch_labels = None
depends_on = None

_TABLE = "audio_program_tracks"
_INDEX = "ix_audio_program_tracks_target"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.create_table(
        _TABLE,
        sa.Column("track_id", sa.String(length=120), primary_key=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("language", sa.String(length=35), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("source_uri", sa.String(length=1000), nullable=True),
        sa.Column("loudness_target_lufs", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('asset', 'channel')", name="audio_program_tracks_scope_check"
        ),
        sa.CheckConstraint(
            "kind IN ('primary', 'sap', 'descriptive')", name="audio_program_tracks_kind_check"
        ),
        schema=schema,
    )
    op.create_index(_INDEX, _TABLE, ["scope", "target_id"], unique=False, schema=schema)


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index(_INDEX, table_name=_TABLE, schema=schema)
    op.drop_table(_TABLE, schema=schema)
