# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S26 subscription paywall: paywall_configs + access_grants + subscriptions.

Three tables for the net-new ``civiccast/paywall/`` module:

* ``paywall_configs`` — one row per station; ``enabled`` is the master
  switch (DC-1). A CHECK pins ``provider`` to ``stripe``/``mock``. A unique
  index on ``station_id`` enforces "one config per station".
* ``access_grants`` — one row per (email, scope) grant. CHECKs pin
  ``scope_kind`` to ``asset``/``series``/``all`` and ``granted_via`` to
  ``subscription``/``comp``/``magic_link``. A composite index on
  ``(station_id, email, scope_kind, scope_id)`` serves the hot-path
  "does this email have access?" lookup; an index on ``subscription_id``
  serves the cascade-on-cancel path.
* ``subscriptions`` — one row per Stripe subscription. ``sub_id`` is the
  Stripe id (we do not mint subscription ids). A CHECK pins ``status`` to
  the four valid Stripe values.

S26 is OPTIONAL / default OFF — a station that never enables it carries
empty tables + zero behavior change (DC-1 cornerstone). The migration
sequences after ``0058_meeting_agenda`` (S25). The ``0056`` slot remains
RESERVED for S21 (scheduled-recording) per RECONCILIATION's chain-shape
footer — when S21 lands, its migration will be a sibling on top of
``0055_asrun_and_epg`` (so ``0055`` will have two children: ``0056`` and
``0057``) and an Alembic merge revision will unify the heads. The linear
path through ``0057 → 0058 → 0059`` is unaffected by that future merge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0059_paywall_access"
down_revision = "0058_meeting_agenda"
branch_labels = None
depends_on = None

_CONFIGS_TABLE = "paywall_configs"
_GRANTS_TABLE = "access_grants"
_SUBS_TABLE = "paywall_subscriptions"
_EVENTS_SEEN_TABLE = "paywall_stripe_events_seen"


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        _CONFIGS_TABLE,
        sa.Column("config_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "provider", sa.String(length=16), nullable=False, server_default=sa.text("'stripe'")
        ),
        sa.Column("tiers", sa.JSON(), nullable=False),
        sa.Column("signing_secret", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('stripe', 'mock')",
            name="paywall_configs_provider_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_paywall_configs_station_unique",
        _CONFIGS_TABLE,
        ["station_id"],
        unique=True,
        schema=schema,
    )

    op.create_table(
        _GRANTS_TABLE,
        sa.Column("grant_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=120), nullable=False, server_default=sa.text("''")),
        sa.Column("granted_via", sa.String(length=16), nullable=False),
        sa.Column("subscription_id", sa.String(length=120), nullable=True),
        sa.Column("magic_link_token_id", sa.String(length=120), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_kind IN ('asset', 'series', 'all')",
            name="access_grants_scope_kind_check",
        ),
        sa.CheckConstraint(
            "granted_via IN ('subscription', 'comp', 'magic_link')",
            name="access_grants_granted_via_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_access_grants_email_scope",
        _GRANTS_TABLE,
        ["station_id", "email", "scope_kind", "scope_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_access_grants_subscription",
        _GRANTS_TABLE,
        ["subscription_id"],
        unique=False,
        schema=schema,
    )
    # Q-10 fix: a unique index on the magic-link token id closes the TOCTOU
    # race where two concurrent verifies could both observe "no grant" and
    # both insert. The PK derivation (``mlg-{token_id}``) collapses to one
    # row today, but this belt-and-suspenders the future case where the
    # grant_id derivation might change. NULL is allowed (subscription /
    # comp grants don't have a token id) and excluded from the constraint
    # by both SQLite and Postgres standard semantics.
    op.create_index(
        "ix_access_grants_magic_link_token_unique",
        _GRANTS_TABLE,
        ["magic_link_token_id"],
        unique=True,
        schema=schema,
    )

    op.create_table(
        _SUBS_TABLE,
        sa.Column("sub_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("tier_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_id", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'past_due', 'canceled', 'incomplete')",
            name="paywall_subscriptions_status_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_paywall_subscriptions_station_email",
        _SUBS_TABLE,
        ["station_id", "email"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_paywall_subscriptions_status",
        _SUBS_TABLE,
        ["status"],
        unique=False,
        schema=schema,
    )

    # Q-1 fix: webhook idempotency ledger. Added IN PLACE to migration 0059
    # (per the S26 GauntletGate directive — no chain split, no 0060). The
    # alembic chain remains: this revision is still ``0059_paywall_access``
    # with ``down_revision = "0058_meeting_agenda"`` and no second
    # revision id; both ``upgrade()`` and ``downgrade()`` create/drop the
    # full bundle atomically.
    #
    # The table records every Stripe event id we've processed (replay
    # protection — Stripe is at-least-once + 5-min signature tolerance).
    # PK on ``event_id`` makes a duplicate INSERT collide; the service
    # treats that collision as a duplicate signal and short-circuits.
    op.create_table(
        _EVENTS_SEEN_TABLE,
        sa.Column("event_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        schema=schema,
    )
    op.create_index(
        "ix_paywall_stripe_events_seen_station",
        _EVENTS_SEEN_TABLE,
        ["station_id"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index(
        "ix_paywall_stripe_events_seen_station", table_name=_EVENTS_SEEN_TABLE, schema=schema
    )
    op.drop_table(_EVENTS_SEEN_TABLE, schema=schema)

    op.drop_index("ix_paywall_subscriptions_status", table_name=_SUBS_TABLE, schema=schema)
    op.drop_index("ix_paywall_subscriptions_station_email", table_name=_SUBS_TABLE, schema=schema)
    op.drop_table(_SUBS_TABLE, schema=schema)

    op.drop_index(
        "ix_access_grants_magic_link_token_unique", table_name=_GRANTS_TABLE, schema=schema
    )
    op.drop_index("ix_access_grants_subscription", table_name=_GRANTS_TABLE, schema=schema)
    op.drop_index("ix_access_grants_email_scope", table_name=_GRANTS_TABLE, schema=schema)
    op.drop_table(_GRANTS_TABLE, schema=schema)

    op.drop_index("ix_paywall_configs_station_unique", table_name=_CONFIGS_TABLE, schema=schema)
    op.drop_table(_CONFIGS_TABLE, schema=schema)
