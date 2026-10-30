# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""0.4.0 migration core: import_batches + import_batch_items.

Two tables for the net-new ``civiccast/migrate/`` module — the provenance
ledger for incumbent-system imports (Cablecast first). No changes to any
other module's tables: imported shows/schedule items land in the existing
``assets`` / ``schedule_items`` tables via the ORM rows those modules
already define; this migration only adds the ledger that records which rows
an import batch created, for exact rollback.

* ``import_batches`` — one row per apply call. ``status`` flips
  ``applied`` -> ``rolled_back`` in place; never deleted (audit trail).
* ``import_batch_items`` — one row per real asset/schedule_item row an
  apply call created, keyed by ``import_batch_id`` (indexed) so rollback
  can look up exactly what to delete.

Sequences after ``0066_hls_sink_kind`` (current chain HEAD).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068_migrate_batches"
down_revision = "0067_agenda_import_provenance"
branch_labels = None
depends_on = None

_BATCHES_TABLE = "import_batches"
_ITEMS_TABLE = "import_batch_items"


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        _BATCHES_TABLE,
        sa.Column("import_batch_id", sa.String(length=64), primary_key=True),
        sa.Column("source_system", sa.String(length=60), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default=sa.text("'applied'")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )

    op.create_table(
        _ITEMS_TABLE,
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("import_batch_id", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_import_batch_items_batch",
        _ITEMS_TABLE,
        ["import_batch_id"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index("ix_import_batch_items_batch", table_name=_ITEMS_TABLE, schema=schema)
    op.drop_table(_ITEMS_TABLE, schema=schema)
    op.drop_table(_BATCHES_TABLE, schema=schema)
