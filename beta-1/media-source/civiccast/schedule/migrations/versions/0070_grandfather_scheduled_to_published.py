# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Grandfather pre-existing scheduled items to published (Commit-to-Air
enforcement, owner decision 2026-07-08).

Revision ID: 0070_grandfather_scheduled_to_published
Revises: 0068_migrate_batches
Create Date: 2026-07-08

The Commit-to-Air gate becomes enforced: only ``published`` schedule items
air (see ``civiccast.egress.source_plan.build_source_plan_from_schedule``).
Before this change, nothing ever wrote ``published`` and every airable
premiere sat in ``scheduled``. A station upgrading mid-flight must not have
its on-air schedule silently stop the moment the gate starts enforcing —
everything already scheduled at upgrade time is treated as pre-approved
(it was, in effect, already airing under the old unenforced rules).

Data migration only: ``UPDATE schedule_items SET state='published' WHERE
state='scheduled'``. Cancelled rows are left untouched.

Downgrade is a documented no-op: there is no way to tell, after the fact,
which ``published`` rows were manually committed, auto-approved by
autoschedule, or grandfathered by this very migration — reverting them all
to ``scheduled`` would silently pull already-approved/aired programs off a
downgraded station. Accepted per the design spec.

Numbered 0070 (not 0069): 0069 is reserved by an in-flight control_room
branch not yet merged into this chain. Revision numbers are repo-global —
parent on the single current head, ``0068_migrate_batches``.
"""

from __future__ import annotations

from alembic import op

revision = "0070_grandfather_scheduled_to_published"
down_revision = "0069_control_room_session_surface_lock"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    table = f"{schema}.schedule_items" if schema else "schedule_items"
    op.execute(
        f"UPDATE {table} SET state = 'published' WHERE state = 'scheduled'"  # noqa: S608 - identifier is code-controlled, not user input  # nosec B608
    )


def downgrade() -> None:
    # ponytail: documented no-op — see the module docstring. There is no
    # data-driven way to distinguish grandfathered/auto-approved/manually
    # committed published rows after the fact, so reverting would be a
    # guess dressed up as a downgrade.
    pass
