# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add control-room device health/freshness + cue versioning columns.

Revision ID: 0064_control_room_health_and_versioning
Revises: 0063_producer_ops
Create Date: 2026-07-05

3.3-to-4.0 sprint item 7 (Control-Room UI) closes three of its six confirmed
gaps at the schema level:

* ``production_devices.last_probed_at`` / ``last_reachable`` — device health
  + state freshness. Written only by ``ControlRoomStore.record_device_probe``
  (a probe/fire attempt), never by the device config write path, so a config
  edit can never masquerade as a fresh health reading.
* ``timeline_cues.version`` — cue versioning, mirroring
  ``device_profiles.version`` (added in migration 0047). Bumped by
  ``ControlRoomStore.upsert_cue`` on every edit to an existing cue.

Following the repo-global single-chain convention: this migration chains off
``0063_producer_ops`` (item 23), which itself chains off
``0062_media_integrity_columns`` (item 5, media-library hardening). Both of
those merged ahead of this PR, so the control-room migration — originally
numbered 0062 in its own worktree — was renumbered to 0064 on merge-prep to
keep the single global chain linear. This is the parallel-worktree numbering
collision the sibling migrations' docstrings flagged as a live risk.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064_control_room_health_and_versioning"
down_revision = "0063_producer_ops"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("production_devices", schema=schema) as batch:
        batch.add_column(sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_reachable", sa.Boolean(), nullable=True))
    with op.batch_alter_table("timeline_cues", schema=schema) as batch:
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    with op.batch_alter_table("timeline_cues", schema=schema) as batch:
        batch.drop_column("version")
    with op.batch_alter_table("production_devices", schema=schema) as batch:
        batch.drop_column("last_reachable")
        batch.drop_column("last_probed_at")
