# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S13 operator AI model selection: ai_model_configuration + feature_model_registry.

Next sequential migration on the single global Alembic chain (ADR 0008); lands on
``0052_secondary_audio``. ``ai_model_configuration`` holds the station-wide config
singleton (created_at/updated_at). ``feature_model_registry`` holds the per-feature
operator selection with a surrogate ``registry_id`` PK and a partial-unique index on
``feature`` WHERE ``deleted_at IS NULL`` (soft-delete aware: a selection can be cleared
and re-created, at most one LIVE row per feature).

The spec's planned number ``0045_ai_model_configuration`` parented on
``0044_loudness_and_eas`` is STALE — the live head is ``0052_secondary_audio`` (S11
shipped per-slice 0049-0052). This migration uses 0053, mirroring how 0051/0052 note
their own renumbering against the spec.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053_ai_model_configuration"
down_revision = "0052_secondary_audio"
branch_labels = None
depends_on = None

_CONFIG_TABLE = "ai_model_configuration"
_REGISTRY_TABLE = "feature_model_registry"
_REGISTRY_INDEX = "feature_model_registry_feature_unique"


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        _CONFIG_TABLE,
        sa.Column("config_id", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )

    op.create_table(
        _REGISTRY_TABLE,
        sa.Column("registry_id", sa.String(length=120), primary_key=True),
        sa.Column("feature", sa.String(length=20), nullable=False),
        sa.Column("model_key", sa.String(length=120), nullable=True),
        sa.Column("tier", sa.String(length=16), nullable=True),
        # Cloud-TOS consent audit (S13 E4/Q3): a billable, content-egressing
        # cloud/frontier selection records who/when accepted; False for local tiers.
        sa.Column(
            "consent_accepted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("consent_actor", sa.String(length=120), nullable=True),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "feature IN ('captions', 'summary', 'translation')",
            name="feature_model_registry_feature_check",
        ),
        sa.CheckConstraint(
            "tier IS NULL OR tier IN ('local', 'cloud', 'frontier')",
            name="feature_model_registry_tier_check",
        ),
        schema=schema,
    )
    # Partial unique: at most one LIVE selection per feature (soft-delete aware).
    op.create_index(
        _REGISTRY_INDEX,
        _REGISTRY_TABLE,
        ["feature"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index(_REGISTRY_INDEX, table_name=_REGISTRY_TABLE, schema=schema)
    op.drop_table(_REGISTRY_TABLE, schema=schema)
    op.drop_table(_CONFIG_TABLE, schema=schema)
