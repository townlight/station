# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Allow operators to cancel and resume offline caption work."""

import sqlalchemy as sa
from alembic import op

revision = "0095_caption_job_cancellation"
down_revision = "0094_cg_live_video_zone"
branch_labels = None
depends_on = None


def _constraint(states: str) -> None:
    schema = None if op.get_bind().dialect.name == "sqlite" else "civiccast"
    with op.batch_alter_table("offline_caption_jobs", schema=schema) as batch:
        batch.drop_constraint("offline_caption_jobs_state_check", type_="check")
        batch.create_check_constraint("offline_caption_jobs_state_check", f"state IN ({states})")


def upgrade() -> None:
    schema = None if op.get_bind().dialect.name == "sqlite" else "civiccast"
    for column in ("translation_source_start_seconds", "translation_source_end_seconds"):
        op.add_column(
            "caption_review_items", sa.Column(column, sa.Float(), nullable=True), schema=schema
        )
    _constraint("'pending', 'awaiting_review', 'complete', 'failed', 'cancelled'")


def downgrade() -> None:
    schema = None if op.get_bind().dialect.name == "sqlite" else "civiccast"
    table = sa.table("offline_caption_jobs", sa.column("state"), schema=schema)
    op.execute(table.update().where(table.c.state == "cancelled").values(state="failed"))
    _constraint("'pending', 'awaiting_review', 'complete', 'failed'")
    with op.batch_alter_table("caption_review_items", schema=schema) as batch:
        batch.drop_column("translation_source_start_seconds")
        batch.drop_column("translation_source_end_seconds")
