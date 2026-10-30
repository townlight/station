# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Allow the hls sink kind on egress sinks.

Revision ID: 0066_hls_sink_kind
Revises: 0065_recording_dropout_fields
Create Date: 2026-07-06

Sprint 0.4 live HLS packaging: the ``hls`` sink tees a rolling live HLS
manifest + segments off the persistent egress encoder into a local
directory (served by ``civiccast.stream.media_router``'s ``/media/live``
mount). The sink kind CHECK constraint must admit it.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

from alembic import op

revision = "0066_hls_sink_kind"
down_revision = "0065_recording_dropout_fields"
branch_labels = None
depends_on = None

_OLD_KINDS = "('srt', 'rtmp', 'local-ts', 'udp-ts', 'file', 'sdi')"
_NEW_KINDS = "('srt', 'rtmp', 'local-ts', 'udp-ts', 'file', 'sdi', 'hls')"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("egress_sinks", schema=schema) as batch:
        batch.drop_constraint("egress_sinks_kind_check", type_="check")
        batch.create_check_constraint("egress_sinks_kind_check", f"kind IN {_NEW_KINDS}")


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    table = f"{schema}.egress_sinks" if schema else "egress_sinks"
    # Rows the old constraint would reject cannot survive the downgrade.
    op.execute(f"DELETE FROM {table} WHERE kind = 'hls'")  # noqa: S608 - identifier is code-controlled, not user input  # nosec B608
    with op.batch_alter_table("egress_sinks", schema=schema) as batch:
        batch.drop_constraint("egress_sinks_kind_check", type_="check")
        batch.create_check_constraint("egress_sinks_kind_check", f"kind IN {_OLD_KINDS}")
