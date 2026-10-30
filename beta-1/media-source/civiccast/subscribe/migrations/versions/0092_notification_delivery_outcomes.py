# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable per-delivery subscriber-notification outcomes (WP-05).

Revision ID: 0092_notification_delivery_outcomes
Revises: 0091_programlog_local_anchor
Create Date: 2026-09-01

``approve_publish`` used to report the subscriber-notifications surface
``succeeded`` after merely BUILDING a payload -- nothing was sent, and nothing
was recorded, so a station had no way to tell a delivered notice from a
non-existent one. These two tables are the receipt that turns that surface
state into an observation:

* ``notification_delivery_outcomes`` -- one LOGICAL delivery per publication x
  subscription x target x transport. The UNIQUE constraint
  ``notification_delivery_outcomes_logical_key`` over exactly those five
  columns is the concurrency/idempotency guard: two approvals racing on the
  same recording cannot create two rows for the same recipient, so the loser
  reads the winner's outcome and skips an already-sent recipient instead of
  mailing them twice. An in-memory guard cannot do that, which is why this is
  a schema change and not a service-layer set.
* ``notification_delivery_attempts`` -- the numbered attempts beneath one
  logical delivery, foreign-keyed to it with ``ON DELETE CASCADE``. The attempt
  number is deliberately NOT part of the logical key.

``lease_expires_at`` on the outcomes table is the second half of the guard: the
UNIQUE constraint settles the INSERT race, and a conditional UPDATE on the lease
settles the already-exists race, so only one caller is ever cleared to send.
A lease that lapses without a recorded result means the sending process died
mid-send, and the row becomes recoverable rather than stuck forever.

PII: stable ids only. ``subscription_id`` is a salted digest; the subscriber's
email address and webhook URL stay sealed in
``subscriptions.encrypted_subscriber_handle`` and are never copied here. No
secret, signature or raw exception text is stored -- ``detail`` carries a short
redacted operator sentence.

Backfill: there is nothing to backfill. No prior release wrote a delivery
receipt of any kind, so every historical publish run's
``subscriber-notifications`` surface legitimately has no row here. Publish
reads that absence as ``unverified`` (never as green evidence) rather than
this migration inventing receipts for sends that never happened --
see ``civiccast.publish.service.build_publish_asset_status``.

Revision numbers are repo-global. This parents on
``0091_programlog_local_anchor``, the single ``alembic heads``
at merge time. The plan sequenced this file as ``0085`` after WP-04's
``0084_podcast_publish_jobs``, which has not landed; the id keeps its planned
slot so WP-04 can take ``0084`` without renumbering, and the ``0084`` gap is
deliberate rather than a missing migration. The single-head guard in
``tests/db/test_migration_graph_guards.py`` fails loudly if a later branch
forks the chain.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0092_notification_delivery_outcomes"
down_revision = "0091_programlog_local_anchor"
branch_labels = None
depends_on = None


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def _qualified(target: str, schema: str | None) -> str:
    return target if schema is None else f"{schema}.{target}"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.create_table(
        "notification_delivery_outcomes",
        sa.Column("delivery_key", sa.String(length=80), primary_key=True),
        sa.Column("publication_id", sa.String(length=200), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("subscription_id", sa.String(length=160), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=120), nullable=False),
        sa.Column("transport", sa.String(length=20), nullable=False),
        sa.Column(
            "outcome", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("retry_id", sa.String(length=120), nullable=True),
        # In-flight lease. While set and in the future, one caller owns this
        # delivery and no other may send it; a lapsed lease with no result
        # means the sender died mid-send and the row is recoverable.
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "publication_id",
            "subscription_id",
            "target_type",
            "target_id",
            "transport",
            name="notification_delivery_outcomes_logical_key",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_notification_delivery_outcomes_publication_id",
        "notification_delivery_outcomes",
        ["publication_id"],
        schema=schema,
    )
    op.create_table(
        "notification_delivery_attempts",
        sa.Column("delivery_key", sa.String(length=80), primary_key=True),
        sa.Column("attempt_number", sa.Integer(), primary_key=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("retry_id", sa.String(length=120), nullable=True),
        # An attempt cannot exist without its logical delivery; deleting the
        # outcome takes its numbered attempts with it.
        sa.ForeignKeyConstraint(
            ["delivery_key"],
            [_qualified("notification_delivery_outcomes.delivery_key", schema)],
            name="notification_delivery_attempts_delivery_key_fkey",
            ondelete="CASCADE",
        ),
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_table("notification_delivery_attempts", schema=schema)
    op.drop_index(
        "ix_notification_delivery_outcomes_publication_id",
        table_name="notification_delivery_outcomes",
        schema=schema,
    )
    op.drop_table("notification_delivery_outcomes", schema=schema)
