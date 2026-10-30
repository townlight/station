# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add source reference to egress proof events."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_egress_proof_source_ref"
down_revision = "0021_egress_health_caption"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.add_column(
        "egress_proof_events",
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_column("egress_proof_events", "source_ref", schema=schema)
