# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S22 user-defined custom metadata fields: custom_field_defs + custom_field_values.

Two tables for the net-new ``civiccast/metadata/`` module:

* ``custom_field_defs`` — operator-defined typed fields, unique per ``(station_id, key)``;
  a CHECK pins ``type`` to the eight supported kinds; an index on ``(station_id, order)``
  drives the ordered admin/editor render.
* ``custom_field_values`` — one asset's value for one field, unique per
  ``(asset_id, field_id)``. ``value`` is a bounded ``String(1000)`` so the composite
  ``(field_id, value)`` index stays under Postgres' B-tree entry cap; ``value_num`` and
  ``value_date`` carry denormalized numeric/date copies indexed for S19/S23 range scans.

Next sequential migration on the single global Alembic chain (ADR 0008); lands on
``0053_ai_model_configuration``. (The spec's planned number ``0051`` is STALE — S11/S13
shipped per-slice through 0053; this is 0054.)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054_custom_metadata_fields"
down_revision = "0053_ai_model_configuration"
branch_labels = None
depends_on = None

_DEFS_TABLE = "custom_field_defs"
_VALUES_TABLE = "custom_field_values"


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        _DEFS_TABLE,
        sa.Column("field_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("searchable", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("api_exposed", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # ``order`` is a SQL reserved word; the column name is quoted by SQLAlchemy.
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("station_id", "key", name="custom_field_defs_station_key_unique"),
        sa.CheckConstraint(
            "type IN ('text', 'longtext', 'list', 'date', 'number', 'boolean', "
            "'asset_ref', 'producer_ref')",
            name="custom_field_defs_type_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_custom_field_defs_station_order",
        _DEFS_TABLE,
        ["station_id", "order"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        _VALUES_TABLE,
        sa.Column("value_id", sa.String(length=160), primary_key=True),
        sa.Column("asset_id", sa.String(length=120), nullable=False),
        sa.Column("field_id", sa.String(length=120), nullable=False),
        sa.Column("value", sa.String(length=1000), nullable=False),
        sa.Column("value_num", sa.Float(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("asset_id", "field_id", name="custom_field_values_asset_field_unique"),
        schema=schema,
    )
    op.create_index(
        "ix_custom_field_values_field_value",
        _VALUES_TABLE,
        ["field_id", "value"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_custom_field_values_field_num",
        _VALUES_TABLE,
        ["field_id", "value_num"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_custom_field_values_field_date",
        _VALUES_TABLE,
        ["field_id", "value_date"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_custom_field_values_asset",
        _VALUES_TABLE,
        ["asset_id"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index("ix_custom_field_values_asset", table_name=_VALUES_TABLE, schema=schema)
    op.drop_index("ix_custom_field_values_field_date", table_name=_VALUES_TABLE, schema=schema)
    op.drop_index("ix_custom_field_values_field_num", table_name=_VALUES_TABLE, schema=schema)
    op.drop_index("ix_custom_field_values_field_value", table_name=_VALUES_TABLE, schema=schema)
    op.drop_table(_VALUES_TABLE, schema=schema)
    op.drop_index("ix_custom_field_defs_station_order", table_name=_DEFS_TABLE, schema=schema)
    op.drop_table(_DEFS_TABLE, schema=schema)
