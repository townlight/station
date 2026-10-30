# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add the S16 production & control-room tables (CivicCast 3.0 — build step 9).

Revision ID: 0047_production_control
Revises: 0046_cg_feed_source_tags
Create Date: 2026-06-16

Seven tables for the external production-switcher control surface (S16):

* ``production_devices`` — a station-owned externally-controlled device
  (obs | vmix | atem | hyperdeck | ptz | osc | tcp | http | casparcg, plus the
  S18 gap-8 ``gpi`` / ``serial`` control kinds). ``secret_ref`` is an opaque
  keyring handle — device credentials are NEVER stored here in cleartext.
* ``device_profiles`` — the TSR mapping/config (device type, non-secret
  options, capability map) + S18 gap-8 Take-Delay / Post-Roll timing; versioned.
* ``control_surfaces`` — a named operator layout gated by ``assigned_role``.
* ``timeline_cues`` — one fireable action (scene/input/transition/macro/deck/
  ptz/osc/http/overlay + gap-8 gpi_pulse/serial_send/router_take).
* ``control_room_sessions`` — a live production session bound to a ``live/``
  program feed.
* ``control_room_cue_events`` — append-only fired-cue audit (redacted).
* ``control_room_device_commands`` — S18 gap-8 GPI/serial/router-take command
  audit with transition timing.

Revision numbering — repo-global single chain (ADR 0008). The real head was
``0046_cg_feed_source_tags``; this takes the next monotonic number and parents
on it. S16 §3 assigned ``0047`` against an assumed ``0046_analytics_viewership``
head — the assumed name was stale, the number happens to match.

No foreign keys: ``device_id`` / ``surface_id`` / ``session_id`` / ``cue_id`` are
soft string references resolved in the store (matching the cg + schedule
modules). An audit row must outlive the entity it describes, and deleting a
device must not cascade into the surfaces/cues that named it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_production_control"
down_revision = "0046_cg_feed_source_tags"
branch_labels = None
depends_on = None

_DEVICE_KINDS = (
    "'obs', 'vmix', 'atem', 'hyperdeck', 'ptz', 'osc', 'tcp', 'http', 'casparcg', 'gpi', 'serial'"
)
_TRANSPORTS = "'tcp', 'udp', 'http', 'websocket', 'serial', 'gpi'"
_CUE_ACTIONS = (
    "'scene', 'input', 'transition', 'macro', 'deck_play', 'deck_cue', "
    "'ptz_preset', 'osc', 'http', 'overlay_push', 'overlay_clear', "
    "'gpi_pulse', 'serial_send', 'router_take'"
)
_SURFACE_ROLES = (
    "'setup_admin', 'meeting_operator', 'records_clerk', 'publish_operator', 'support_admin'"
)
_SESSION_STATES = "'open', 'closed'"
_CUE_RESULTS = "'planned', 'fired', 'failed'"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.create_table(
        "production_devices",
        sa.Column("device_id", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("transport", sa.String(length=20), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("secret_ref", sa.String(length=200), nullable=True),
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
        sa.PrimaryKeyConstraint("device_id", name="production_devices_pkey"),
        sa.CheckConstraint(f"kind IN ({_DEVICE_KINDS})", name="production_devices_kind_check"),
        sa.CheckConstraint(
            f"transport IN ({_TRANSPORTS})", name="production_devices_transport_check"
        ),
        schema=schema,
    )

    op.create_table(
        "device_profiles",
        sa.Column("profile_id", sa.String(length=120), nullable=False),
        sa.Column("device_id", sa.String(length=120), nullable=False),
        sa.Column("tsr_device_type", sa.String(length=60), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("capability_map", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("take_delay_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("post_roll_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
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
        sa.PrimaryKeyConstraint("profile_id", name="device_profiles_pkey"),
        schema=schema,
    )
    op.create_index("ix_device_profiles_device", "device_profiles", ["device_id"], schema=schema)

    op.create_table(
        "control_surfaces",
        sa.Column("surface_id", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column(
            "assigned_role",
            sa.String(length=40),
            nullable=False,
            server_default="meeting_operator",
        ),
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
        sa.PrimaryKeyConstraint("surface_id", name="control_surfaces_pkey"),
        sa.CheckConstraint(
            f"assigned_role IN ({_SURFACE_ROLES})", name="control_surfaces_role_check"
        ),
        schema=schema,
    )

    op.create_table(
        "timeline_cues",
        sa.Column("cue_id", sa.String(length=120), nullable=False),
        sa.Column("surface_id", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("device_id", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "confirm_required", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("bank", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("proof_boundary", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("cue_id", name="timeline_cues_pkey"),
        sa.CheckConstraint(f"action IN ({_CUE_ACTIONS})", name="timeline_cues_action_check"),
        schema=schema,
    )
    op.create_index("ix_timeline_cues_surface", "timeline_cues", ["surface_id"], schema=schema)
    op.create_index("ix_timeline_cues_device", "timeline_cues", ["device_id"], schema=schema)

    op.create_table(
        "control_room_sessions",
        sa.Column("session_id", sa.String(length=120), nullable=False),
        sa.Column("surface_id", sa.String(length=120), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=False),
        sa.Column("operator_name", sa.String(length=200), nullable=True),
        sa.Column("program_feed_source_ref", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("session_id", name="control_room_sessions_pkey"),
        sa.CheckConstraint(
            f"state IN ({_SESSION_STATES})", name="control_room_sessions_state_check"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_control_room_sessions_surface",
        "control_room_sessions",
        ["surface_id"],
        schema=schema,
    )

    op.create_table(
        "control_room_cue_events",
        sa.Column("event_id", sa.String(length=120), nullable=False),
        sa.Column("session_id", sa.String(length=120), nullable=False),
        sa.Column("cue_id", sa.String(length=120), nullable=False),
        sa.Column("operator_id", sa.String(length=120), nullable=False),
        sa.Column("device_id", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("detail", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.PrimaryKeyConstraint("event_id", name="control_room_cue_events_pkey"),
        sa.CheckConstraint(
            f"result IN ({_CUE_RESULTS})", name="control_room_cue_events_result_check"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_control_room_cue_events_session",
        "control_room_cue_events",
        ["session_id"],
        schema=schema,
    )

    op.create_table(
        "control_room_device_commands",
        sa.Column("command_id", sa.String(length=120), nullable=False),
        sa.Column("device_id", sa.String(length=120), nullable=False),
        sa.Column("session_id", sa.String(length=120), nullable=True),
        sa.Column("command_kind", sa.String(length=30), nullable=False),
        sa.Column("command_preview", sa.String(length=500), nullable=False),
        sa.Column("take_delay_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("post_roll_ms", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("issued_by", sa.String(length=120), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("result", sa.String(length=20), nullable=False, server_default="planned"),
        sa.PrimaryKeyConstraint("command_id", name="control_room_device_commands_pkey"),
        sa.CheckConstraint(
            f"result IN ({_CUE_RESULTS})", name="control_room_device_commands_result_check"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_control_room_device_commands_device",
        "control_room_device_commands",
        ["device_id"],
        schema=schema,
    )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None
    op.drop_index(
        "ix_control_room_device_commands_device",
        table_name="control_room_device_commands",
        schema=schema,
    )
    op.drop_table("control_room_device_commands", schema=schema)
    op.drop_index(
        "ix_control_room_cue_events_session",
        table_name="control_room_cue_events",
        schema=schema,
    )
    op.drop_table("control_room_cue_events", schema=schema)
    op.drop_index(
        "ix_control_room_sessions_surface", table_name="control_room_sessions", schema=schema
    )
    op.drop_table("control_room_sessions", schema=schema)
    op.drop_index("ix_timeline_cues_device", table_name="timeline_cues", schema=schema)
    op.drop_index("ix_timeline_cues_surface", table_name="timeline_cues", schema=schema)
    op.drop_table("timeline_cues", schema=schema)
    op.drop_table("control_surfaces", schema=schema)
    op.drop_index("ix_device_profiles_device", table_name="device_profiles", schema=schema)
    op.drop_table("device_profiles", schema=schema)
    op.drop_table("production_devices", schema=schema)
