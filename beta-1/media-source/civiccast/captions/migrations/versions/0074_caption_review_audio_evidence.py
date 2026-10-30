# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Retain private live-audio evidence for caption review items.

Revision ID: 0074_caption_review_audio_evidence
Revises: 0073_egress_allow_software_fallback
Create Date: 2026-07-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074_caption_review_audio_evidence"
down_revision = "0073_egress_allow_software_fallback"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("caption_review_items", schema=schema) as batch:
        batch.add_column(sa.Column("audio_evidence_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("audio_evidence_start_seconds", sa.Float(), nullable=True))
        batch.add_column(sa.Column("audio_evidence_sha256", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("audio_evidence_bytes", sa.BigInteger(), nullable=True))
        batch.create_check_constraint(
            "caption_review_items_audio_evidence_check",
            "("
            "audio_evidence_path IS NULL AND "
            "audio_evidence_start_seconds IS NULL AND "
            "audio_evidence_sha256 IS NULL AND "
            "audio_evidence_bytes IS NULL"
            ") OR ("
            "audio_evidence_path IS NOT NULL AND "
            "audio_evidence_start_seconds >= 0 AND "
            "audio_evidence_sha256 IS NOT NULL AND "
            "audio_evidence_bytes > 0"
            ")",
        )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("caption_review_items", schema=schema) as batch:
        batch.drop_constraint(
            "caption_review_items_audio_evidence_check",
            type_="check",
        )
        batch.drop_column("audio_evidence_bytes")
        batch.drop_column("audio_evidence_sha256")
        batch.drop_column("audio_evidence_start_seconds")
        batch.drop_column("audio_evidence_path")
