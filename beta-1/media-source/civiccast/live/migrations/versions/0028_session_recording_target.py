# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Stamp the resolved recording target on live sessions.

Revision ID: 0028_session_recording_target
Revises: 0027_asset_disposition_reviews
Create Date: 2026-06-10

Beta sprint B1 (decision #5): provenance by construction. ``go_on_air``
records which recording target the session will use; the finalization worker
reads the stamp instead of guessing from the global target list (audit
ENG-005 durable fix; the rehearsal-target exclusion remains as the legacy
fallback for unstamped sessions).

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_session_recording_target"
down_revision = "0027_asset_disposition_reviews"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "live_sessions",
        sa.Column("recording_target_id", sa.String(length=64), nullable=True),
        schema=schema,
    )
    op.add_column(
        "live_sessions",
        sa.Column("recording_target_uri", sa.Text(), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("live_sessions", schema=schema) as batch:
        batch.drop_column("recording_target_uri")
        batch.drop_column("recording_target_id")
