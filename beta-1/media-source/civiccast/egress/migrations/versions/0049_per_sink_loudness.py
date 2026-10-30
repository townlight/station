# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S11b per-sink loudness: per-destination loudness regime on ``egress_sinks``.

The incumbent PEG workflow normalizes per destination (cable -24 LKFS / streaming -16 LUFS from
one show); S11 decision 1 puts the same control on each ``EgressSinkSpec``.
Adds four columns to ``egress_sinks``:

* ``loudness_regime`` (streaming/atsc-a85/ebu-r128/inherit) + a CHECK guard,
* ``loudness_target_lufs`` / ``loudness_tolerance_lufs`` (explicit override),
* ``eas_tone_strip_enabled`` (S11 gap-B; default on, applied to OTT sinks in
  slice 3).

Server defaults mirror ``inherit`` / NULL / true so rows written before 0049
read back exactly as they did before the columns existed (back-compat: an
``inherit`` sink resolves to the channel ``EgressConfig.loudness_target_lufs``,
which is today's single-target behaviour).

Next sequential migration on the single global Alembic chain (ADR 0008); lands
on the current head ``0048_remote_contribution``. The spec's planned
``0044_loudness_and_eas`` number is stale (0044 is ``cg_board_designer``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_per_sink_loudness"
down_revision = "0048_remote_contribution"
branch_labels = None
depends_on = None

_REGIME_CHECK = "egress_sinks_loudness_regime_check"
_REGIME_SQL = "loudness_regime IN ('streaming', 'atsc-a85', 'ebu-r128', 'inherit')"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    with op.batch_alter_table("egress_sinks", schema=schema) as batch_op:
        batch_op.add_column(
            sa.Column(
                "loudness_regime",
                sa.String(length=16),
                nullable=False,
                server_default="inherit",
            )
        )
        batch_op.add_column(sa.Column("loudness_target_lufs", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("loudness_tolerance_lufs", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "eas_tone_strip_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            )
        )
        batch_op.create_check_constraint(_REGIME_CHECK, _REGIME_SQL)


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    with op.batch_alter_table("egress_sinks", schema=schema) as batch_op:
        batch_op.drop_constraint(_REGIME_CHECK, type_="check")
        batch_op.drop_column("eas_tone_strip_enabled")
        batch_op.drop_column("loudness_tolerance_lufs")
        batch_op.drop_column("loudness_target_lufs")
        batch_op.drop_column("loudness_regime")
