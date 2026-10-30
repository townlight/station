# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add a language dimension to caption review items.

Revision ID: 0083_caption_review_language
Revises: 0082_egress_graphics_overlay
Create Date: 2026-08-31

Recorded-Spanish captions (owner requirement: a published recording must
carry an operator-reviewed Spanish track alongside English). English
transcription rows and Spanish translation rows share one ``asset_id`` but
must be reviewed as two separate passes, so the review queue gains a
``language`` column. Non-null with an ``en`` server default so every
pre-existing row backfills as English -- the language every prior review row
implicitly was.

Revision numbers are repo-global; this parents on the current single head,
``0082_egress_graphics_overlay`` (which lives in ``civiccast/egress/``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083_caption_review_language"
down_revision = "0082_egress_graphics_overlay"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("caption_review_items", schema=schema) as batch:
        batch.add_column(
            sa.Column(
                "language",
                sa.String(length=12),
                nullable=False,
                server_default="en",
            )
        )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("caption_review_items", schema=schema) as batch:
        batch.drop_column("language")
