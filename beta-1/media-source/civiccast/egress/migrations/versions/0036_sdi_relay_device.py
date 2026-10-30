# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the per-channel SDI relay device to egress configs.

Revision ID: 0036_sdi_relay_device
Revises: 0035_ndi_relay_name
Create Date: 2026-06-12

Issue #117 (BYO-SDI, option c): ``sdi_relay_device`` is the durable operator
intent — NULL means no SDI output; a DeckLink device name means the channel
automation driver supervises an SDI relay feeding the channel's output to
that card through the station's own DeckLink-capable FFmpeg build.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_sdi_relay_device"
down_revision = "0035_ndi_relay_name"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "egress_configs",
        sa.Column("sdi_relay_device", sa.String(200), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("egress_configs", "sdi_relay_device", schema=schema)
