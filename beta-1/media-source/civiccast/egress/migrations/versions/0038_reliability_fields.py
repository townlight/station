# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S9 reliability fields: health schema-currency/churn + proof-event index.

Adds to ``egress_health_samples`` a schema-version stamp and a proof-event churn
count for operator visibility (the System Health schema-drift badge), and a composite
index on ``egress_proof_events(channel_id, observed_at)`` so the per-tick churn cap +
``count_proof_events_since`` + ``recent_proof_events`` are index range scans rather than
full scans of an append-only table.

Co-process durable-pid columns were deliberately NOT added here: the SDI-descope
(3.0 = IP-only) left them with no 3.0 writer/reader, so they ship WITH their consumer
in build step 7 (CasparCG device co-process), not as dead schema now.

Next sequential migration on the single global chain (ADR 0008); the spec's planned
0042/0043 numbers are unbuilt — this lands on the real head ``0037_asset_meeting_body``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_reliability_fields"
down_revision = "0037_asset_meeting_body"
branch_labels = None
depends_on = None

_PROOF_INDEX = "ix_egress_proof_events_channel_observed"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    with op.batch_alter_table("egress_health_samples", schema=schema) as batch_op:
        batch_op.add_column(
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(
            sa.Column("proof_events_appended", sa.Integer(), nullable=False, server_default="0")
        )
    op.create_index(
        _PROOF_INDEX,
        "egress_proof_events",
        ["channel_id", "observed_at"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index(_PROOF_INDEX, table_name="egress_proof_events", schema=schema)
    with op.batch_alter_table("egress_health_samples", schema=schema) as batch_op:
        batch_op.drop_column("proof_events_appended")
        batch_op.drop_column("schema_version")
