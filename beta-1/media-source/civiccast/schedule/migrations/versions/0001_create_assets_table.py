# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""create assets table

Revision ID: 0001_create_assets_table
Revises:
Create Date: 2026-05-09

First per-module migration for civiccast.schedule. Creates the
``civiccast.assets`` table mirroring the SQLAlchemy
:class:`civiccast.schedule.models.Asset` declarative model.

Per ADR 0008 §Compliance + CLAUDE.md Tooling clause: both ``upgrade`` and
``downgrade`` are implemented and exercised by automated test (see
``tests/schedule/test_migration_reversibility.py`` and
``tests/schedule/test_real_postgres.py``).

The schema qualifier (``schema="civiccast"``) is honored on Postgres
(the production target) and silently ignored on SQLite (the fast-test
path) — ``alembic/env.py`` suppresses ``include_schemas`` on SQLite so
the table lands as plain ``assets`` in the SQLite namespace.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_create_assets_table"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    """Return True if the active dialect supports schemas (Postgres).

    SQLite ignores schema qualifiers; passing ``schema="civiccast"`` to
    :func:`op.create_table` on SQLite raises ``unknown database`` unless
    the schema is ATTACHed. The fast-test path uses plain unqualified
    table names; production Postgres uses the qualified name.
    """
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    """Create the ``civiccast.assets`` table.

    Columns mirror ``civiccast.schedule.models.Asset`` exactly. The
    schema qualifier is conditionally applied so the migration runs
    cleanly on both Postgres and SQLite reversibility tests.
    """
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "assets",
        sa.Column("asset_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("manifest_url", sa.Text(), nullable=False),
        sa.Column("poster_url", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    """Drop the ``civiccast.assets`` table.

    Symmetric to :func:`upgrade`. Required by CLAUDE.md's Tooling clause
    ("both ``upgrade`` and ``downgrade`` implemented and tested") and
    exercised by ``test_migration_reversibility.py`` and
    ``test_real_postgres.py``.
    """
    schema = "civiccast" if _use_schema() else None
    op.drop_table("assets", schema=schema)
