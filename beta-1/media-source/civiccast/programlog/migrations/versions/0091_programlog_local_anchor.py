# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persist a program slot's original local calendar and wall-clock anchor.

Legacy rows remain nullable because their previous station timezone was not
stored. The materializer adopts the current station zone before generating
more occurrences; a profile timezone edit pins under the previous zone first.
"""

import sqlalchemy as sa
from alembic import op

revision = "0091_programlog_local_anchor"
down_revision = "0090_egress_command_history_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = None if op.get_bind().dialect.name == "sqlite" else "civiccast"
    op.add_column(
        "channel_program_slots",
        sa.Column("recurrence_anchor_local", sa.DateTime(timezone=False), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = None if op.get_bind().dialect.name == "sqlite" else "civiccast"
    op.drop_column("channel_program_slots", "recurrence_anchor_local", schema=schema)
