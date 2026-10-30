# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Widen assets_state_check to include 'recorded'

Revision ID: 0006_widen_asset_state_check
Revises: 0005_schema_hardening_audit_v030
Create Date: 2026-05-11

Sprint 0.4 Slice 1 (Broadcast Spine And Contracts) - adds 'recorded' to
the assets.state CHECK constraint so the live-broadcast finalization
path can transition a recorded asset into the asset library at state
'recorded'.

Through v0.3.1 the constraint allowed only:

    pending_ingest, ingesting, validated, rejected

After this migration:

    pending_ingest, ingesting, validated, rejected, recorded

The SA model's __table_args__ at civiccast/schedule/models.py:388-391
is updated in the same Slice 1 Commit 2 so SQLite test paths exercise
the widened CHECK at create_all() time.

Per ADR 0008: both upgrade and downgrade implemented.

Downgrade safety: downgrade refuses to narrow the constraint while
'recorded' rows exist. Re-imposing the narrower CHECK with 'recorded'
rows present would either fail at constraint-validation time (Postgres)
or silently allow invalid rows to persist with a narrower constraint
that misrepresents the data. The downgrade body explicitly counts
recorded rows first and raises a clear RuntimeError if any exist,
leaving the widened constraint in place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_widen_asset_state_check"
down_revision: str | None = "0005_schema_hardening_audit_v030"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


_OLD_STATES_SQL = "state IN ('pending_ingest', 'ingesting', 'validated', 'rejected')"
_NEW_STATES_SQL = "state IN ('pending_ingest', 'ingesting', 'validated', 'rejected', 'recorded')"


def upgrade() -> None:
    """Widen assets.state CHECK to include 'recorded'.

    SQLite path: no-op at the DDL level; SQLite test paths use
    Base.metadata.create_all from the updated SA model, so the widened
    CHECK ships through __table_args__ automatically. Postgres path:
    drop the named CHECK and recreate it with the wider state set.
    """
    if not _use_schema():
        return

    schema = "civiccast"

    op.drop_constraint(
        "assets_state_check",
        "assets",
        type_="check",
        schema=schema,
    )
    op.create_check_constraint(
        "assets_state_check",
        "assets",
        _NEW_STATES_SQL,
        schema=schema,
    )


def downgrade() -> None:
    """Narrow assets.state CHECK back to the original four-state set.

    Refuses to run while any assets.state = 'recorded' rows exist. Re-
    imposing the narrower CHECK with such rows present would either
    fail Postgres's constraint-validation at recreate time or leave the
    database in an internally inconsistent state. Operators downgrading
    past this migration must first re-state or delete recorded rows.
    """
    if not _use_schema():
        return

    schema = "civiccast"

    recorded_count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM civiccast.assets WHERE state = 'recorded'")
    )
    if recorded_count and recorded_count > 0:
        raise RuntimeError(
            "Refusing to downgrade past 0006_widen_asset_state_check: "
            f"{recorded_count} asset row(s) currently have state='recorded'. "
            "Re-state or delete recorded assets before downgrading. "
            "The widened CHECK constraint is left in place; no schema change made."
        )

    op.drop_constraint(
        "assets_state_check",
        "assets",
        type_="check",
        schema=schema,
    )
    op.create_check_constraint(
        "assets_state_check",
        "assets",
        _OLD_STATES_SQL,
        schema=schema,
    )
