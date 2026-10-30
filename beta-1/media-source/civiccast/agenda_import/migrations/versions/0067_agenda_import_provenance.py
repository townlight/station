# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""4.1.0 Agenda Bridge Phase 1: agenda_import_provenance table.

Revision ID: 0067_agenda_import_provenance
Revises: 0066_hls_sink_kind
Create Date: 2026-07-07

One new, additive table owned entirely by the ``civiccast/agenda_import/``
package -- no columns are added to ``meeting_agendas`` and
``civiccast/agenda/models.py`` / ``store.py`` are not touched. Resolves plan
§5 Open Question 5 ("should CivicCast persist import provenance?") as "yes,
via a side table": one row per ``agenda_id`` recording which vendor
source/client/external event it was last imported from, enabling a future
"refresh from source" button. This table is bookkeeping only -- import
idempotency itself is enforced by ``civiccast.agenda_import.mapper`` via the
existing ``agenda_items (agenda_id, order)`` unique constraint, not by
anything here.

Revision numbers are repo-global — parent on the single current head.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0067_agenda_import_provenance"
down_revision = "0066_hls_sink_kind"
branch_labels = None
depends_on = None

_TABLE = "agenda_import_provenance"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.create_table(
        _TABLE,
        sa.Column("agenda_id", sa.String(length=120), primary_key=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("client_code", sa.String(length=120), nullable=False),
        sa.Column("external_id", sa.String(length=120), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_table(_TABLE, schema=schema)
