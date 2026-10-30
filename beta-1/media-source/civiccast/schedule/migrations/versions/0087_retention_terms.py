# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""WP-08: value/unit/forever retention-term authoring.

Revision ID: 0087_retention_terms
Revises: 0086_live_source_probe_state
Create Date: 2026-09-02

Finalization plan (``implementation-plan.md`` section 6/7) reserves this
migration id as ``0087_retention_terms``, chained after ``0086`` in the
plan's schema-sequencing table (``0083`` Spanish, ``0084`` podcast,
``0085`` subscriber outcomes, ``0086`` live-source probe -- WP-02/04/05/07
respectively). ``0084`` (podcast) and ``0085`` (subscriber outcomes) never
landed; ``0083_caption_review_language`` (#131) and
``0086_live_source_probe_state`` (#140) did, and this migration now
chains ``down_revision`` to the real ``0086_live_source_probe_state``
head. WP-08's dependency on ``0086`` was always a migration-BASE
ordering requirement, not a functional dependency on WP-07's live-source
work (finalization plan section 6, WP-08's own "Dependency" line).

Adds three columns to ``assets``, additive to the existing
``retention_policy``/``retention_until`` pair (unchanged; the enforcement
worker -- ``civiccast.schedule.retention_worker`` -- still reads only
those two and is NOT modified by this migration):

* ``retention_term_unit``    -- ``days`` | ``weeks`` | ``months`` |
  ``years`` | ``forever`` | NULL (NULL = never authored under the new
  contract; a "legacy" row per the WP-08 plan).
* ``retention_term_value``   -- positive integer for a finite unit; NULL
  for ``forever`` or for a legacy row.
* ``retention_anchor_at``    -- immutable once captured: the instant this
  asset was FIRST published. See
  ``civiccast.schedule.models.Asset.retention_anchor_at`` for the full
  "why not ``published_at``" rationale (unpublish clears it; republish
  overwrites it).

Backfill (safe, non-inventive subset only -- finalization plan section 6,
items 4-5):

* ``retention_policy = 'permanent'`` rows: unambiguous under the new
  contract (permanent means "keep forever", no numeric value, no anchor
  needed since ``forever`` never computes a deadline) -- backfilled to
  ``retention_term_unit = 'forever'``.
* Any row with a non-NULL ``published_at``: backfilled
  ``retention_anchor_at = published_at`` -- the best available real
  publication signal already on the row, not an invented one. An asset
  currently unpublished (``published_at IS NULL``) gets no backfilled
  anchor; if it converts to the new contract later with no anchor at all,
  the store sets-and-audits one at conversion time (see
  ``civiccast.schedule.store.PostgresAssetStore._apply_retention_term``).
* Legacy ``short``/``default``/``meeting`` rows are deliberately NOT
  auto-converted -- the plan explicitly forbids inventing a duration or
  anchor for the ambiguous ones, and even ``short``'s well-known 30-day
  meaning is only ever offered as an operator-facing prefill SUGGESTION
  (``civiccast.schedule.retention_terms.LEGACY_SHORT_SUGGESTED_VALUE``),
  never written to a row without an explicit operator conversion action.

Downgrade drops all three columns; the backfill is not reversible data
(the source ``retention_policy``/``published_at`` values it was computed
from are untouched, so nothing is lost).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0087_retention_terms"
down_revision: str | None = "0086_live_source_probe_state"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _schema() -> str | None:
    return "civiccast" if _use_schema() else None


def _assets_table(schema: str | None) -> str:
    return f"{schema}.assets" if schema else "assets"


def upgrade() -> None:
    schema = _schema()
    table = _assets_table(schema)

    op.add_column(
        "assets",
        sa.Column("retention_term_unit", sa.String(length=10), nullable=True),
        schema=schema,
    )
    op.add_column(
        "assets",
        sa.Column("retention_term_value", sa.Integer(), nullable=True),
        schema=schema,
    )
    op.add_column(
        "assets",
        sa.Column("retention_anchor_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )

    if _use_schema():
        # SQLite: no support for ALTER-adding a CHECK constraint to an
        # existing table (same posture as 0080_watch_folder_daemon /
        # 0006_widen_asset_state_check). SQLite test paths build the
        # schema from Base.metadata.create_all, which already carries
        # these CHECKs via Asset.__table_args__ -- only a real ALTER-based
        # upgrade against an existing Postgres database needs this block.
        op.create_check_constraint(
            "assets_retention_term_unit_check",
            "assets",
            "retention_term_unit IS NULL OR retention_term_unit IN "
            "('days', 'weeks', 'months', 'years', 'forever')",
            schema=schema,
        )
        op.create_check_constraint(
            "assets_retention_term_value_check",
            "assets",
            "retention_term_unit IS NULL "
            "OR (retention_term_unit = 'forever' AND retention_term_value IS NULL) "
            "OR (retention_term_unit != 'forever' AND retention_term_value IS NOT NULL "
            "AND retention_term_value > 0)",
            schema=schema,
        )

    # Backfill 1: permanent -> forever. Unambiguous, no anchor required.
    op.execute(
        f"UPDATE {table} SET retention_term_unit = 'forever', retention_term_value = NULL "  # noqa: S608 - identifier is code-controlled, not user input  # nosec B608
        "WHERE retention_policy = 'permanent'"
    )
    # Backfill 2: reuse the real published_at as the anchor for any asset
    # that has one. Never invents a value for an asset that was never
    # published.
    op.execute(
        f"UPDATE {table} SET retention_anchor_at = published_at "  # noqa: S608 - identifier is code-controlled, not user input  # nosec B608
        "WHERE published_at IS NOT NULL AND retention_anchor_at IS NULL"
    )


def downgrade() -> None:
    schema = _schema()
    if _use_schema():
        op.drop_constraint(
            "assets_retention_term_value_check", "assets", schema=schema, type_="check"
        )
        op.drop_constraint(
            "assets_retention_term_unit_check", "assets", schema=schema, type_="check"
        )
    op.drop_column("assets", "retention_anchor_at", schema=schema)
    op.drop_column("assets", "retention_term_value", schema=schema)
    op.drop_column("assets", "retention_term_unit", schema=schema)
