# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Enforce the control-room "one open session per surface" lock at the DB level.

Legacy code-review finding (control_room, high severity): the operator lock
was enforced only by an application-level check-then-insert in
``ControlRoomService.open_session`` (a SELECT for an existing open session,
then an INSERT with no transaction spanning both) -- so two concurrent
``open_session`` calls for the same surface could both pass the check and
both succeed, leaving two operators believing they each exclusively hold the
surface.

Adds a partial-unique index on ``control_room_sessions.surface_id`` filtered
to ``state = 'open'``, mirroring the existing
``ix_equipment_checkouts_one_open_per_item`` pattern from
``0063_producer_ops``. ``ControlRoomStore.open_session`` now catches the
resulting ``IntegrityError`` and raises a clean ``SessionSurfaceConflictError``
instead of a raw DB error.

Sequences after ``0068_migrate_batches`` (current chain HEAD).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069_control_room_session_surface_lock"
down_revision = "0068_migrate_batches"
branch_labels = None
depends_on = None

_TABLE = "control_room_sessions"
_INDEX = "ix_control_room_sessions_one_open_per_surface"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_index(
        _INDEX,
        _TABLE,
        ["surface_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("state = 'open'"),
        postgresql_where=sa.text("state = 'open'"),
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(_INDEX, table_name=_TABLE, schema=schema)
