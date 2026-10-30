# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Keep live agenda markers associated with their eventual captured asset."""

import sqlalchemy as sa
from alembic import op

revision = "0096_agenda_recording_link"
down_revision = "0095_caption_job_cancellation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    with op.batch_alter_table("meeting_agendas", schema=schema) as batch:
        batch.add_column(sa.Column("source_recording_job_id", sa.String(120), nullable=True))
        batch.create_unique_constraint(
            "meeting_agendas_station_job_unique", ["station_id", "source_recording_job_id"]
        )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    with op.batch_alter_table("meeting_agendas", schema=schema) as batch:
        batch.drop_constraint("meeting_agendas_station_job_unique", type_="unique")
        batch.drop_column("source_recording_job_id")
