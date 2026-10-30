# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S11a caption decode-back proofs: ``egress_caption_proof_samples``.

Persists one row per live CEA-608/708 decode-back check (the emitted stream is
decoded and its captions compared to the expected cues). The daemon's
caption_status_provider reads the latest PASS within a freshness window so a
health sample's ``caption_status`` reflects PROVEN captions, not a hardcoded
posture. Rolling/capped per channel (S9 churn discipline).

Next sequential migration on the single global Alembic chain (ADR 0008); lands on
``0049_per_sink_loudness``. (The spec's planned ``0044_loudness_and_eas`` combined
number is stale; S11 ships per-slice migrations: 0049 loudness, 0050 caption
proofs, then the EAS + secondary-audio tables in later slices.)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_caption_proof_samples"
down_revision = "0049_per_sink_loudness"
branch_labels = None
depends_on = None

_TABLE = "egress_caption_proof_samples"
_INDEX = "ix_egress_caption_proof_samples_channel_sampled"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.create_table(
        _TABLE,
        sa.Column("sample_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("caption_status", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("decoder_name", sa.String(length=120), nullable=False),
        sa.Column("expected_cue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decoded_cue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("matched_cue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_timing_delta_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("proof_boundary", sa.String(length=160), nullable=False),
        sa.Column("blocker", sa.String(length=200), nullable=True),
        sa.CheckConstraint(
            "status IN ('PASS', 'FAIL')",
            name="egress_caption_proof_samples_status_check",
        ),
        sa.CheckConstraint(
            "caption_status IN ('not-verified', 'on')",
            name="egress_caption_proof_samples_caption_status_check",
        ),
        schema=schema,
    )
    op.create_index(_INDEX, _TABLE, ["channel_id", "sampled_at"], unique=False, schema=schema)


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index(_INDEX, table_name=_TABLE, schema=schema)
    op.drop_table(_TABLE, schema=schema)
