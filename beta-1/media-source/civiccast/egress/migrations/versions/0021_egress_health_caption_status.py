# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add caption status to egress health samples."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_egress_health_caption"
down_revision = "0020_egress_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    with op.batch_alter_table("egress_health_samples", schema=schema) as batch_op:
        batch_op.add_column(
            sa.Column(
                "caption_status",
                sa.String(length=32),
                nullable=False,
                server_default="not-verified",
            )
        )
        batch_op.create_check_constraint(
            "egress_health_samples_caption_status_check",
            "caption_status IN ('not-verified', 'on')",
        )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    with op.batch_alter_table("egress_health_samples", schema=schema) as batch_op:
        batch_op.drop_constraint(
            "egress_health_samples_caption_status_check",
            type_="check",
        )
        batch_op.drop_column("caption_status")
