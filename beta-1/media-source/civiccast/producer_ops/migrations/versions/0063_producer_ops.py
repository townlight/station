# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Item 23: producer/volunteer/equipment operations tables.

Six tables for the net-new ``civiccast/producer_ops/`` module, completing
the four item-23 pieces already shipped by ``civiccast/contribute/``
(producer accounts, show submission, rights/release metadata, approval
queue):

* ``series_applications`` — a producer's request for a recurring series
  slot (distinct from one-off show submission). A CHECK pins ``state``
  to submitted/under_review/approved/declined.
* ``volunteer_roles`` — the volunteer roster.
* ``call_sheets`` + ``call_sheet_assignments`` — a shoot's crew plan and
  its per-volunteer role assignments.
* ``equipment_items`` + ``equipment_checkouts`` — the equipment roster
  and its checkout/return ledger. A CHECK pins ``state`` to
  checked_out/returned; a partial-unique index on
  ``equipment_checkouts.equipment_id`` (filtered to
  ``state = 'checked_out'``) enforces "at most one open checkout per
  item" at the database level.
* ``training_badges`` — badges a volunteer has earned.
* ``equipment_access_rules`` — the badge required to check out an
  equipment item; a unique index on ``equipment_id`` enforces at most
  one rule per item.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_producer_ops"
down_revision = "0062_media_integrity_columns"
branch_labels = None
depends_on = None

_SERIES_APPLICATIONS = "series_applications"
_VOLUNTEER_ROLES = "volunteer_roles"
_CALL_SHEETS = "call_sheets"
_CALL_SHEET_ASSIGNMENTS = "call_sheet_assignments"
_EQUIPMENT_ITEMS = "equipment_items"
_EQUIPMENT_CHECKOUTS = "equipment_checkouts"
_TRAINING_BADGES = "training_badges"
_EQUIPMENT_ACCESS_RULES = "equipment_access_rules"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        _SERIES_APPLICATIONS,
        sa.Column("application_id", sa.String(length=120), primary_key=True),
        sa.Column("contributor_id", sa.String(length=120), nullable=False),
        sa.Column("series_title", sa.String(length=240), nullable=False),
        sa.Column("proposed_cadence", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=4000), nullable=False),
        sa.Column(
            "state", sa.String(length=20), nullable=False, server_default=sa.text("'submitted'")
        ),
        sa.Column("review_notes", sa.String(length=2000), nullable=True),
        sa.Column("series_id", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('submitted', 'under_review', 'approved', 'declined')",
            name="series_applications_state_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_series_applications_contributor",
        _SERIES_APPLICATIONS,
        ["contributor_id"],
        schema=schema,
    )

    op.create_table(
        _VOLUNTEER_ROLES,
        sa.Column("volunteer_id", sa.String(length=120), primary_key=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("role_name", sa.String(length=120), nullable=False),
        sa.Column("contact_email", sa.String(length=320), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )

    op.create_table(
        _CALL_SHEETS,
        sa.Column("call_sheet_id", sa.String(length=120), primary_key=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("shoot_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.String(length=300), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )

    op.create_table(
        _CALL_SHEET_ASSIGNMENTS,
        sa.Column("assignment_id", sa.String(length=120), primary_key=True),
        sa.Column("call_sheet_id", sa.String(length=120), nullable=False),
        sa.Column("volunteer_id", sa.String(length=120), nullable=False),
        sa.Column("role_name", sa.String(length=120), nullable=False),
        sa.Column("call_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_call_sheet_assignments_call_sheet",
        _CALL_SHEET_ASSIGNMENTS,
        ["call_sheet_id"],
        schema=schema,
    )
    op.create_index(
        "ix_call_sheet_assignments_volunteer",
        _CALL_SHEET_ASSIGNMENTS,
        ["volunteer_id"],
        schema=schema,
    )

    op.create_table(
        _EQUIPMENT_ITEMS,
        sa.Column("equipment_id", sa.String(length=120), primary_key=True),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )

    op.create_table(
        _EQUIPMENT_CHECKOUTS,
        sa.Column("checkout_id", sa.String(length=120), primary_key=True),
        sa.Column("equipment_id", sa.String(length=120), nullable=False),
        sa.Column("volunteer_id", sa.String(length=120), nullable=False),
        sa.Column(
            "state", sa.String(length=20), nullable=False, server_default=sa.text("'checked_out'")
        ),
        sa.Column("checked_out_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            "state IN ('checked_out', 'returned')",
            name="equipment_checkouts_state_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_equipment_checkouts_equipment",
        _EQUIPMENT_CHECKOUTS,
        ["equipment_id"],
        schema=schema,
    )
    op.create_index(
        "ix_equipment_checkouts_volunteer",
        _EQUIPMENT_CHECKOUTS,
        ["volunteer_id"],
        schema=schema,
    )
    # Partial-unique: at most one OPEN checkout per equipment item, enforced
    # at the database level (not just app-layer) so a race between two
    # concurrent checkout requests for the same item cannot both succeed.
    op.create_index(
        "ix_equipment_checkouts_one_open_per_item",
        _EQUIPMENT_CHECKOUTS,
        ["equipment_id"],
        unique=True,
        schema=schema,
        sqlite_where=sa.text("state = 'checked_out'"),
        postgresql_where=sa.text("state = 'checked_out'"),
    )

    op.create_table(
        _TRAINING_BADGES,
        sa.Column("badge_id", sa.String(length=120), primary_key=True),
        sa.Column("volunteer_id", sa.String(length=120), nullable=False),
        sa.Column("badge_name", sa.String(length=120), nullable=False),
        sa.Column("earned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.create_index(
        "ix_training_badges_volunteer",
        _TRAINING_BADGES,
        ["volunteer_id"],
        schema=schema,
    )

    op.create_table(
        _EQUIPMENT_ACCESS_RULES,
        sa.Column("rule_id", sa.String(length=120), primary_key=True),
        sa.Column("equipment_id", sa.String(length=120), nullable=False),
        sa.Column("required_badge_name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_equipment_access_rules_equipment_unique",
        _EQUIPMENT_ACCESS_RULES,
        ["equipment_id"],
        unique=True,
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.drop_index(
        "ix_equipment_access_rules_equipment_unique",
        table_name=_EQUIPMENT_ACCESS_RULES,
        schema=schema,
    )
    op.drop_table(_EQUIPMENT_ACCESS_RULES, schema=schema)

    op.drop_index("ix_training_badges_volunteer", table_name=_TRAINING_BADGES, schema=schema)
    op.drop_table(_TRAINING_BADGES, schema=schema)

    op.drop_index(
        "ix_equipment_checkouts_one_open_per_item",
        table_name=_EQUIPMENT_CHECKOUTS,
        schema=schema,
    )
    op.drop_index(
        "ix_equipment_checkouts_volunteer", table_name=_EQUIPMENT_CHECKOUTS, schema=schema
    )
    op.drop_index(
        "ix_equipment_checkouts_equipment", table_name=_EQUIPMENT_CHECKOUTS, schema=schema
    )
    op.drop_table(_EQUIPMENT_CHECKOUTS, schema=schema)

    op.drop_table(_EQUIPMENT_ITEMS, schema=schema)

    op.drop_index(
        "ix_call_sheet_assignments_volunteer",
        table_name=_CALL_SHEET_ASSIGNMENTS,
        schema=schema,
    )
    op.drop_index(
        "ix_call_sheet_assignments_call_sheet",
        table_name=_CALL_SHEET_ASSIGNMENTS,
        schema=schema,
    )
    op.drop_table(_CALL_SHEET_ASSIGNMENTS, schema=schema)

    op.drop_table(_CALL_SHEETS, schema=schema)

    op.drop_table(_VOLUNTEER_ROLES, schema=schema)

    op.drop_index(
        "ix_series_applications_contributor", table_name=_SERIES_APPLICATIONS, schema=schema
    )
    op.drop_table(_SERIES_APPLICATIONS, schema=schema)
