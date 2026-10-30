# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live-takeover audit table + takeover/handback command actions (3.0 — S5 slice 1).

Revision ID: 0042_takeover_audit_and_command_action
Revises: 0041_commit_rollback_fields
Create Date: 2026-06-15

S5 (Software Force Matrix) wires the proven live-takeover engine. Slice 1 adds:
(a) the ``takeover_audit`` table — one durable row per manual takeover→handback
cycle (``returned_at`` NULL while live); and (b) extends the
``egress_commands`` action CHECK to admit ``takeover`` / ``handback`` so the
daemon can be commanded to invoke ``supervisor.request_live_takeover`` /
``request_live_handback``.

Repo-global single chain — parents on the REAL head ``0041_commit_rollback_fields``
(the spec text said ``0039``, but ``0038``/``0039`` were taken by the S9/S8 work
and ``0040``/``0041`` by S4; this takes the next monotonic number, ``0042``).

Altering a CHECK on SQLite requires a table rebuild via ``op.batch_alter_table``
(mirrors ``0034_udp_ts_sink_kind``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_takeover_audit_and_command_action"
down_revision = "0041_commit_rollback_fields"
branch_labels = None
depends_on = None

_OLD_ACTIONS = "('start', 'stop', 'reload', 'drain')"
_NEW_ACTIONS = "('start', 'stop', 'reload', 'drain', 'takeover', 'handback')"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "takeover_audit",
        sa.Column("session_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=80), nullable=False),
        sa.Column("source_ref", sa.String(length=160), nullable=False),
        sa.Column("source_label", sa.String(length=160), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=False),
        sa.Column("operator_name", sa.String(length=160), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("took_over_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_plan_json", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("session_id", name="takeover_audit_pkey"),
        schema=schema,
    )
    op.create_index(
        "ix_takeover_audit_channel_took_over",
        "takeover_audit",
        ["channel_id", "took_over_at"],
        schema=schema,
    )
    with op.batch_alter_table("egress_commands", schema=schema) as batch:
        batch.drop_constraint("egress_commands_action_check", type_="check")
        batch.create_check_constraint("egress_commands_action_check", f"action IN {_NEW_ACTIONS}")


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    table = f"{schema}.egress_commands" if schema else "egress_commands"
    # Rows the old constraint would reject cannot survive the downgrade.
    op.execute(f"DELETE FROM {table} WHERE action IN ('takeover', 'handback')")  # noqa: S608 - identifier is code-controlled, not user input  # nosec B608
    with op.batch_alter_table("egress_commands", schema=schema) as batch:
        batch.drop_constraint("egress_commands_action_check", type_="check")
        batch.create_check_constraint("egress_commands_action_check", f"action IN {_OLD_ACTIONS}")
    op.drop_index(
        "ix_takeover_audit_channel_took_over",
        table_name="takeover_audit",
        schema=schema,
    )
    op.drop_table("takeover_audit", schema=schema)
