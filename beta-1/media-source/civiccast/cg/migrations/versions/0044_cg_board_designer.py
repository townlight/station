# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the CG bulletin-board designer tables (CivicCast 3.0 — S6 V1, step 7).

Revision ID: 0044_cg_board_designer
Revises: 0043_scheduling_automation
Create Date: 2026-06-16

Closes PEG automation coverage gap 6's authoring layer (S6). Five tables:

* ``cg_boards`` — a durable per-channel board binding a template (one active
  board per channel, enforced in the store).
* ``cg_zone_configs`` — a zone within a board and its content source
  (feed_adapter | manual | schedule | emergency | image | clock).
* ``cg_feed_sources`` — a registered dynamic feed (rss | ical | caldav |
  weather | social) with a refresh policy, trust tier, and last-fetch state.
* ``cg_board_audit`` — append-only board-lifecycle event log.
* ``cg_feed_item_approvals`` — operator approvals of individual feed items for
  approval-gated zones.

Revision numbering — repo-global single chain (ADR 0008). The real head was
``0043_scheduling_automation`` (S18); this takes the next monotonic number and
parents on it. S6 §3 assigned ``0040``, but that number was taken by S4's
commit-to-air migration — the spec number was stale.

No foreign keys: ``channel_id`` / ``board_id`` / ``feed_source_id`` are soft
string references resolved in the store (matching ``cg_bulletins`` and
``schedule_items``). A board audit row must outlive the board it describes, and
deleting a feed must not cascade into the zones that named it — the resolver
degrades such a zone instead of failing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_cg_board_designer"
down_revision = "0043_scheduling_automation"
branch_labels = None
depends_on = None

_ZONE_KINDS = "'primary', 'ticker', 'schedule', 'logo', 'sponsor', 'audio', 'alert'"
_REGIONS = "'main', 'lower', 'side', 'bug', 'background'"
_CONTENT_SOURCES = "'feed_adapter', 'manual', 'schedule', 'emergency', 'image', 'clock'"
_FEED_KINDS = "'rss', 'ical', 'caldav', 'weather', 'social'"
_TRUST_TIERS = "'operator_curated', 'partner_curated', 'public_permitted'"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        "cg_boards",
        sa.Column("board_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("template_id", sa.String(length=120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("board_id", name="cg_boards_pkey"),
        schema=schema,
    )
    op.create_index("ix_cg_boards_channel", "cg_boards", ["channel_id"], schema=schema)

    op.create_table(
        "cg_zone_configs",
        sa.Column("zone_id", sa.String(length=120), nullable=False),
        sa.Column("board_id", sa.String(length=120), nullable=False),
        sa.Column("region", sa.String(length=50), nullable=False),
        sa.Column("zone_kind", sa.String(length=20), nullable=False),
        sa.Column("content_source", sa.String(length=20), nullable=False),
        sa.Column("feed_source_id", sa.String(length=120), nullable=True),
        sa.Column("refresh_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "approval_required", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("manual_text", sa.Text(), nullable=True),
        sa.Column("image_asset_ref", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("zone_id", name="cg_zone_configs_pkey"),
        sa.CheckConstraint(f"zone_kind IN ({_ZONE_KINDS})", name="cg_zone_configs_kind_check"),
        sa.CheckConstraint(f"region IN ({_REGIONS})", name="cg_zone_configs_region_check"),
        sa.CheckConstraint(
            f"content_source IN ({_CONTENT_SOURCES})", name="cg_zone_configs_source_check"
        ),
        schema=schema,
    )
    op.create_index("ix_cg_zone_configs_board", "cg_zone_configs", ["board_id"], schema=schema)

    op.create_table(
        "cg_feed_sources",
        sa.Column("feed_source_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=False),
        sa.Column("trust_tier", sa.String(length=30), nullable=False),
        sa.Column("refresh_seconds", sa.Integer(), nullable=False, server_default=sa.text("900")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fetch_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("feed_source_id", name="cg_feed_sources_pkey"),
        sa.CheckConstraint(f"kind IN ({_FEED_KINDS})", name="cg_feed_sources_kind_check"),
        sa.CheckConstraint(f"trust_tier IN ({_TRUST_TIERS})", name="cg_feed_sources_trust_check"),
        schema=schema,
    )
    op.create_index("ix_cg_feed_sources_channel", "cg_feed_sources", ["channel_id"], schema=schema)

    op.create_table(
        "cg_board_audit",
        sa.Column("audit_id", sa.String(length=120), nullable=False),
        sa.Column("board_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("event_kind", sa.String(length=50), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("audit_id", name="cg_board_audit_pkey"),
        schema=schema,
    )
    op.create_index("ix_cg_board_audit_board", "cg_board_audit", ["board_id"], schema=schema)
    op.create_index("ix_cg_board_audit_channel", "cg_board_audit", ["channel_id"], schema=schema)

    op.create_table(
        "cg_feed_item_approvals",
        sa.Column("approval_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("feed_source_id", sa.String(length=120), nullable=False),
        sa.Column("item_id", sa.String(length=120), nullable=False),
        sa.Column("approved_by_operator", sa.String(length=120), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("approval_id", name="cg_feed_item_approvals_pkey"),
        sa.UniqueConstraint(
            "channel_id",
            "feed_source_id",
            "item_id",
            name="uq_cg_feed_item_approvals_item",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_cg_feed_item_approvals_feed",
        "cg_feed_item_approvals",
        ["channel_id", "feed_source_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_cg_feed_item_approvals_feed", table_name="cg_feed_item_approvals", schema=schema
    )
    op.drop_table("cg_feed_item_approvals", schema=schema)
    op.drop_index("ix_cg_board_audit_channel", table_name="cg_board_audit", schema=schema)
    op.drop_index("ix_cg_board_audit_board", table_name="cg_board_audit", schema=schema)
    op.drop_table("cg_board_audit", schema=schema)
    op.drop_index("ix_cg_feed_sources_channel", table_name="cg_feed_sources", schema=schema)
    op.drop_table("cg_feed_sources", schema=schema)
    op.drop_index("ix_cg_zone_configs_board", table_name="cg_zone_configs", schema=schema)
    op.drop_table("cg_zone_configs", schema=schema)
    op.drop_index("ix_cg_boards_channel", table_name="cg_boards", schema=schema)
    op.drop_table("cg_boards", schema=schema)
