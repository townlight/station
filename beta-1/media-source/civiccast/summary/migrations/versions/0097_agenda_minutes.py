# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Preserve agenda minutes and edit snapshots in the existing summary row."""

import sqlalchemy as sa
from alembic import op

revision = "0097_agenda_minutes"
down_revision = "0096_agenda_recording_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = "civiccast" if op.get_bind().dialect.name != "sqlite" else None
    op.add_column(
        "summaries", sa.Column("agenda_minutes_json", sa.Text(), nullable=True), schema=schema
    )


def downgrade() -> None:
    schema = "civiccast" if op.get_bind().dialect.name != "sqlite" else None
    with op.batch_alter_table("summaries", schema=schema) as batch:
        batch.drop_column("agenda_minutes_json")
