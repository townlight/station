# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Make uploaded assets' manifest_url nullable on SQLite too.

Revision ID: 0018_manifest_url_nullable
Revises: 0017_activitypub_full_federation
Create Date: 2026-05-23

Migration 0002 relaxed ``assets.manifest_url`` on Postgres, but SQLite cannot
alter a column in place. That left installer-managed SQLite databases with the
original NOT NULL constraint even though the ORM and upload flow correctly
store ``manifest_url=None`` until packaging finishes.
"""

from __future__ import annotations

from alembic import op

revision = "0018_manifest_url_nullable"
down_revision = "0017_activitypub_full_federation"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    if schema is not None:
        # Postgres was already relaxed by migration 0002. This revision only
        # repairs Alembic-created SQLite databases, where 0002 could not alter
        # the original NOT NULL column in place.
        return
    with op.batch_alter_table("assets") as batch_op:
        batch_op.alter_column("manifest_url", nullable=True, existing_type=None)


def downgrade() -> None:
    schema = _schema()
    if schema is not None:
        return
    with op.batch_alter_table("assets") as batch_op:
        batch_op.alter_column("manifest_url", nullable=False, existing_type=None)
