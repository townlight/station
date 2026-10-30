# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the operator-facing graphics-overlay toggle + lower-third text to egress configs.

Revision ID: 0082_egress_graphics_overlay
Revises: 0081_summary_generation_jobs
Create Date: 2026-08-30

``graphics_overlay_enabled`` / ``graphics_overlay_lower_third_text`` are the durable
operator control-plane fields wired to the engine graphics-overlay leg (PR #93's
``station_bug_and_lower_third_leg`` / ``GraphicsOverlayLeg``). Both default off/blank
(False / empty string) so an existing channel's persisted config -- and the playout
graph built from it -- is unaffected until an operator opts in.

Revision numbers are repo-global -- parent on the single current head
(``alembic heads`` at authoring time: ``0081_summary_generation_jobs``, the
``civiccast.summary`` module's most recent migration).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0082_egress_graphics_overlay"
down_revision = "0081_summary_generation_jobs"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "egress_configs",
        sa.Column(
            "graphics_overlay_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema=schema,
    )
    op.add_column(
        "egress_configs",
        sa.Column(
            "graphics_overlay_lower_third_text",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("egress_configs", "graphics_overlay_lower_third_text", schema=schema)
    op.drop_column("egress_configs", "graphics_overlay_enabled", schema=schema)
