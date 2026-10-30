# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the per-channel NDI relay name to egress configs.

Revision ID: 0035_ndi_relay_name
Revises: 0034_udp_ts_sink_kind
Create Date: 2026-06-11

Issue #116 (BYO-NDI, option c): ``ndi_relay_name`` is the durable operator
intent — NULL means no NDI output; a name means the channel automation
driver supervises an NDI relay publishing the channel's output under that
name through the station's own NDI-capable FFmpeg build.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_ndi_relay_name"
down_revision = "0034_udp_ts_sink_kind"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "egress_configs",
        sa.Column("ndi_relay_name", sa.String(200), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("egress_configs", "ndi_relay_name", schema=schema)
