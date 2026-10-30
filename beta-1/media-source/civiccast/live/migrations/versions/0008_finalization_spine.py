# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""create live_session_events table and link asset to live session

Revision ID: 0008_finalization_spine
Revises: 0007_live_sessions
Create Date: 2026-05-11

Sprint 0.4 Slice 1 Commit 7 - recording-finalization data spine.

Adds two pieces of schema that together let the live module land a
finalized recording as an asset row at state ``recorded`` atomically:

1. ``live_session_events`` -- typed audit row per live-session event.
   Composite primary key on ``(live_session_id, event_type, event_seq)``
   guarantees idempotency: a duplicate ``session.finalized`` event for
   the same session collides on the PK and the finalization transaction
   rolls back, so the caller's retry returns the pre-existing asset
   row rather than creating a second one.

   Event types CHECKed at the DB level: ``session.started``,
   ``session.ended``, ``session.finalized``. Slice 1 Commit 7 only
   emits ``session.finalized``; future commits may emit the other two
   at the matching state transitions (the schema is general so the
   later commit needs no DDL).

2. ``assets.source_live_session_id`` -- nullable FK column linking a
   recorded asset back to the live session that produced it, plus a
   partial unique index ``assets_source_live_session_unique`` enforcing
   "at most one asset per source live session" even if an application-
   layer caller bypasses the event uniqueness guard. The partial index
   ignores rows where ``source_live_session_id IS NULL`` so existing
   uploaded assets are unaffected.

Per ADR 0008: both upgrade and downgrade implemented. The downgrade
refuses to drop ``live_session_events`` while finalized event rows
exist (mirrors the 0006 downgrade-safety pattern) and refuses to drop
``assets.source_live_session_id`` while assets with that column set
exist.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_finalization_spine"
down_revision: str | None = "0007_live_sessions"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _server_default_now() -> sa.sql.elements.TextClause:
    """Dialect-appropriate ``now()`` default expression.

    Postgres: ``now()``. SQLite: ``CURRENT_TIMESTAMP``. The test path
    uses SA's ``create_all`` against the SA model rather than running
    this migration, but keeping both dialects honest in the migration
    body protects fresh-deploy operators on Postgres.
    """
    if _use_schema():
        return sa.text("now()")
    return sa.text("CURRENT_TIMESTAMP")


# Names referenced from both upgrade + downgrade so a rename never
# drifts between the two halves.
_EVENT_TYPE_CHECK_SQL = "event_type IN ('session.started', 'session.ended', 'session.finalized')"
_EVENT_SEQ_CHECK_SQL = "event_seq >= 1"


def upgrade() -> None:
    """Create ``live_session_events`` and link ``assets`` to live sessions."""
    schema = "civiccast" if _use_schema() else None
    now_default = _server_default_now()

    op.create_table(
        "live_session_events",
        sa.Column("live_session_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now_default,
        ),
        sa.PrimaryKeyConstraint(
            "live_session_id",
            "event_type",
            "event_seq",
            name="live_session_events_pkey",
        ),
        sa.CheckConstraint(_EVENT_TYPE_CHECK_SQL, name="live_session_events_event_type_check"),
        sa.CheckConstraint(_EVENT_SEQ_CHECK_SQL, name="live_session_events_event_seq_positive"),
        schema=schema,
    )

    # Add the FK column to assets. Nullable because uploaded assets have
    # no source live session; only finalization-derived assets carry it.
    op.add_column(
        "assets",
        sa.Column("source_live_session_id", sa.String(length=64), nullable=True),
        schema=schema,
    )

    # Partial unique index: at most one asset per source live session.
    # Postgres native partial index; SQLite supports the same partial-
    # index syntax for the test-side SA create_all() path.
    op.create_index(
        "assets_source_live_session_unique",
        "assets",
        ["source_live_session_id"],
        unique=True,
        postgresql_where=sa.text("source_live_session_id IS NOT NULL"),
        sqlite_where=sa.text("source_live_session_id IS NOT NULL"),
        schema=schema,
    )


def downgrade() -> None:
    """Drop the column + index + table, with safety guards on operator data.

    Refuses to drop while live finalization data exists; an operator who
    really wants to downgrade past this migration must first delete the
    relevant rows. Mirrors the 0006 downgrade-safety pattern.
    """
    schema_str = "civiccast" if _use_schema() else None
    schema_prefix = f"{schema_str}." if schema_str else ""

    if _use_schema():
        # ``schema_prefix`` is constructed from ``schema_str`` which is
        # the hardcoded literal ``"civiccast"`` -- there is no user
        # input path into this string. ruff S608 cannot reason about
        # that, so silence inline.
        recorded_assets = op.get_bind().scalar(
            sa.text(
                f"SELECT count(*) FROM {schema_prefix}assets "  # noqa: S608  # nosec B608
                "WHERE source_live_session_id IS NOT NULL"
            )
        )
        if recorded_assets and recorded_assets > 0:
            raise RuntimeError(
                "Refusing to downgrade past 0008_finalization_spine: "
                f"{recorded_assets} asset row(s) carry source_live_session_id values. "
                "Delete or null the column on these rows before downgrading. "
                "Schema is left unchanged."
            )

        event_rows = op.get_bind().scalar(
            sa.text(f"SELECT count(*) FROM {schema_prefix}live_session_events")  # noqa: S608  # nosec B608
        )
        if event_rows and event_rows > 0:
            raise RuntimeError(
                "Refusing to downgrade past 0008_finalization_spine: "
                f"{event_rows} live_session_events row(s) exist. Delete them before "
                "downgrading. Schema is left unchanged."
            )

    op.drop_index(
        "assets_source_live_session_unique",
        table_name="assets",
        schema=schema_str,
    )
    op.drop_column("assets", "source_live_session_id", schema=schema_str)
    op.drop_table("live_session_events", schema=schema_str)
