# SPDX-License-Identifier: Apache-2.0
"""v1.2 staff token lifecycle persistence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_staff_tokens_v12"
down_revision = "0015_podcast_v08"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "civiccast" if op.get_bind().dialect.name != "sqlite" else None


def upgrade() -> None:
    schema = _schema()
    op.create_table(
        "staff_tokens",
        sa.Column("token_id", sa.String(length=80), primary_key=True),
        sa.Column("operator_id", sa.String(length=160), nullable=False),
        sa.Column("operator_display_name", sa.String(length=200), nullable=False),
        sa.Column("token_hash", sa.String(length=256), nullable=False),
        sa.Column("salt_b64", sa.String(length=80), nullable=False),
        sa.Column("hash_algorithm", sa.String(length=40), nullable=False),
        sa.Column("hash_iterations", sa.Integer(), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column("rotated_from_token_id", sa.String(length=80), nullable=True),
        schema=schema,
    )
    op.create_index(
        "ix_staff_tokens_operator",
        "staff_tokens",
        ["operator_id", "revoked_at"],
        schema=schema,
    )
    op.create_table(
        "staff_token_audit_events",
        sa.Column("event_id", sa.String(length=80), primary_key=True),
        sa.Column("token_id", sa.String(length=80), nullable=True),
        sa.Column("operator_id", sa.String(length=160), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_staff_token_audit_token",
        "staff_token_audit_events",
        ["token_id", "created_at"],
        schema=schema,
    )


def downgrade() -> None:
    schema = _schema()
    op.drop_index(
        "ix_staff_token_audit_token",
        table_name="staff_token_audit_events",
        schema=schema,
    )
    op.drop_table("staff_token_audit_events", schema=schema)
    op.drop_index("ix_staff_tokens_operator", table_name="staff_tokens", schema=schema)
    op.drop_table("staff_tokens", schema=schema)
