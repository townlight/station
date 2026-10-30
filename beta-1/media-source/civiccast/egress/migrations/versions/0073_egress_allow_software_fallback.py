# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the allow_software_fallback operator opt-in to egress configs.

Revision ID: 0073_egress_allow_software_fallback
Revises: 0072_normalize_recording_file_uris
Create Date: 2026-07-23

``allow_software_fallback`` is the durable operator opt-in that permits CPU
(software) encoding when no hardware encoder is present for a channel.
Default False: without this flag, a channel with no available hardware
encoder fails loud instead of silently falling back to software encoding.

Revision numbers are repo-global — parent on the single current head.
Renumbered 0072 -> 0073 on this branch: main independently published a
DIFFERENT 0072 (0072_normalize_recording_file_uris). This reconciliation
re-chains 0073 onto that migration, preserving the single Alembic head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073_egress_allow_software_fallback"
down_revision = "0072_normalize_recording_file_uris"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "egress_configs",
        sa.Column(
            "allow_software_fallback",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("egress_configs", "allow_software_fallback", schema=schema)
