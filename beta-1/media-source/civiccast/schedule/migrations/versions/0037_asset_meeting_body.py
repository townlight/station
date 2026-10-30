# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the meeting-body category tag to assets.

Revision ID: 0037_asset_meeting_body
Revises: 0036_sdi_relay_device
Create Date: 2026-06-12

Option b for the #107 remainder: residents browse recordings by the meeting
body they belong to (e.g. "City Council", "School Board"). ``meeting_body``
is a nullable operator-set tag; NULL = untagged. The portal derives its
browse facet from the values actually in use — no fixed vocabulary table.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_asset_meeting_body"
down_revision = "0036_sdi_relay_device"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.add_column(
        "assets",
        sa.Column("meeting_body", sa.String(120), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_column("assets", "meeting_body", schema=schema)
