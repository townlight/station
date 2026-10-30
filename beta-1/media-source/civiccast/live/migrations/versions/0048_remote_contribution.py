# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the S17 remote-contribution tables (CivicCast 3.0 — build step 9).

Revision ID: 0048_remote_contribution
Revises: 0047_production_control
Create Date: 2026-06-16

Three tables for the VDO.Ninja remote-guest contribution tier (S17):

* ``contribution_rooms`` — a named, channel-scoped WebRTC room (the VDO.Ninja
  room the operator publishes guests into). ``state`` idle|open|live|closing|
  closed; ``compositor_target`` gst_compositor (V1 default) | obs_browser_source.
* ``guest_invites`` — a single-use, expiring invite to ONE remote participant.
  ``invite_token`` is uniquely indexed (the public capability + the single-use
  consume guard). ``role`` is a *contribution* role (council_member | presenter
  | public_comment), NOT an auth role.
* ``remote_guest_sessions`` — the live per-guest connection record + its state
  machine (invited → joining → connected → on_air → muted → dropped → ended),
  with ``admitted_at`` for the waiting-room hold (Scott decision S17 §10.6).

Revision numbering — repo-global single chain (ADR 0008). The real head was
``0047_production_control`` (S16); this takes the next monotonic number and
parents on it, matching the S17 §3 assignment of ``0048``.

No foreign keys: ``room_id`` / ``invite_id`` are soft string references resolved
in the store (matching the cg / schedule / control_room modules). A session/
invite row must outlive the room it named; deleting a room must not cascade.

**No co-edit of ``live_sources_source_type_check``** — S17 V1 reuses the existing
``ndi`` / ``srt`` ``LiveSource`` kinds for the composited guest output (Scott
decision S17 §10.3); no ``"webrtc"`` kind is added.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_remote_contribution"
down_revision = "0047_production_control"
branch_labels = None
depends_on = None

_ROOM_STATES = "'idle', 'open', 'live', 'closing', 'closed'"
_COMPOSITOR_TARGETS = "'obs_browser_source', 'gst_compositor'"
_CONTRIBUTION_ROLES = "'council_member', 'presenter', 'public_comment'"
_GUEST_SESSION_STATES = "'invited', 'joining', 'connected', 'on_air', 'muted', 'dropped', 'ended'"
_CONNECTION_QUALITIES = "'unknown', 'good', 'degraded', 'poor'"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        "contribution_rooms",
        sa.Column("room_id", sa.String(length=120), nullable=False),
        sa.Column("channel_id", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("vdo_room_name", sa.String(length=200), nullable=False),
        sa.Column("max_guests", sa.Integer(), nullable=False, server_default=sa.text("6")),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="idle"),
        sa.Column(
            "compositor_target",
            sa.String(length=30),
            nullable=False,
            server_default="gst_compositor",
        ),
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
        sa.PrimaryKeyConstraint("room_id", name="contribution_rooms_pkey"),
        sa.CheckConstraint(f"state IN ({_ROOM_STATES})", name="contribution_rooms_state_check"),
        sa.CheckConstraint(
            f"compositor_target IN ({_COMPOSITOR_TARGETS})",
            name="contribution_rooms_compositor_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_contribution_rooms_channel", "contribution_rooms", ["channel_id"], schema=schema
    )

    op.create_table(
        "guest_invites",
        sa.Column("invite_id", sa.String(length=120), nullable=False),
        sa.Column("room_id", sa.String(length=120), nullable=False),
        sa.Column("guest_display_name", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("invite_token", sa.String(length=200), nullable=False),
        sa.Column("push_url", sa.String(length=1000), nullable=True),
        sa.Column("view_url", sa.String(length=1000), nullable=True),
        sa.Column("terms_agreement_id", sa.String(length=120), nullable=True),
        sa.Column("terms_version", sa.String(length=40), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("invite_id", name="guest_invites_pkey"),
        sa.UniqueConstraint("invite_token", name="guest_invites_token_key"),
        sa.CheckConstraint(f"role IN ({_CONTRIBUTION_ROLES})", name="guest_invites_role_check"),
        schema=schema,
    )
    op.create_index("ix_guest_invites_room", "guest_invites", ["room_id"], schema=schema)

    op.create_table(
        "remote_guest_sessions",
        sa.Column("session_id", sa.String(length=120), nullable=False),
        sa.Column("room_id", sa.String(length=120), nullable=False),
        sa.Column("invite_id", sa.String(length=120), nullable=False),
        sa.Column("guest_display_name", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="invited"),
        sa.Column(
            "connection_quality",
            sa.String(length=20),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("on_air_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proof_boundary", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name="remote_guest_sessions_pkey"),
        sa.CheckConstraint(
            f"state IN ({_GUEST_SESSION_STATES})", name="remote_guest_sessions_state_check"
        ),
        sa.CheckConstraint(
            f"connection_quality IN ({_CONNECTION_QUALITIES})",
            name="remote_guest_sessions_quality_check",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_remote_guest_sessions_room", "remote_guest_sessions", ["room_id"], schema=schema
    )
    op.create_index(
        "ix_remote_guest_sessions_invite", "remote_guest_sessions", ["invite_id"], schema=schema
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_remote_guest_sessions_invite", table_name="remote_guest_sessions", schema=schema
    )
    op.drop_index(
        "ix_remote_guest_sessions_room", table_name="remote_guest_sessions", schema=schema
    )
    op.drop_table("remote_guest_sessions", schema=schema)
    op.drop_index("ix_guest_invites_room", table_name="guest_invites", schema=schema)
    op.drop_table("guest_invites", schema=schema)
    op.drop_index("ix_contribution_rooms_channel", table_name="contribution_rooms", schema=schema)
    op.drop_table("contribution_rooms", schema=schema)
