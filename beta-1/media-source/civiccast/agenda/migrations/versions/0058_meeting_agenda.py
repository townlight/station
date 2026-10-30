# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S25 meeting agenda integration: meeting_agendas + agenda_items.

Two tables for the net-new ``civiccast/agenda/`` module:

* ``meeting_agendas`` — one row per (station, meeting asset) agenda. A CHECK
  pins ``status`` to ``draft``/``published``. A unique constraint on
  ``(station_id, meeting_asset_id)`` enforces "one agenda per meeting" so the
  public endpoint's single-row lookup by asset can never return ambiguous
  results.
* ``agenda_items`` — ordered items under an agenda. Unique on
  ``(agenda_id, order)`` so operator-defined ordering is canonical without
  a tiebreaker. Two indexes serve the sidebar (by order) and the player
  chapter list (by timecode).

This migration sequences after ``0057_underwriting_spots`` (S24); ``0058``
is the current chain HEAD (linear chain ``0054 → 0055 → 0057 → 0058``). The
slot ``0056`` remains RESERVED for S21 (scheduled-recording) per RECONCILIATION
D17 + the chain-shape footer (the paragraph under §11's migration-assignments
table). When S21 lands, its migration will declare
``down_revision = "0055_asrun_and_epg"``, creating a single sibling branch off
``0055`` (so ``0055`` will then have TWO children: ``0056`` and ``0057``); an
Alembic merge revision will then unify the two heads — ``0058`` (the
linear-chain head) and ``0056`` (the new sibling head). The linear path
through ``0057_underwriting_spots → 0058_meeting_agenda`` is unaffected by
that future merge.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0058_meeting_agenda"
down_revision = "0057_underwriting_spots"
branch_labels = None
depends_on = None

_AGENDAS_TABLE = "meeting_agendas"
_ITEMS_TABLE = "agenda_items"


def upgrade() -> None:
    schema = op.get_context().version_table_schema

    op.create_table(
        _AGENDAS_TABLE,
        sa.Column("agenda_id", sa.String(length=120), primary_key=True),
        sa.Column("station_id", sa.String(length=120), nullable=False),
        sa.Column("meeting_asset_id", sa.String(length=120), nullable=False),
        sa.Column("source_doc_url", sa.String(length=2000), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'draft'")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'published')",
            name="meeting_agendas_status_check",
        ),
        sa.UniqueConstraint(
            "station_id", "meeting_asset_id", name="meeting_agendas_station_asset_unique"
        ),
        schema=schema,
    )
    op.create_index(
        "ix_meeting_agendas_station",
        _AGENDAS_TABLE,
        ["station_id"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_meeting_agendas_asset",
        _AGENDAS_TABLE,
        ["meeting_asset_id"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        _ITEMS_TABLE,
        sa.Column("item_id", sa.String(length=120), primary_key=True),
        sa.Column("agenda_id", sa.String(length=120), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("number", sa.String(length=40), nullable=True),
        sa.Column("title", sa.String(length=400), nullable=False),
        sa.Column("video_timecode_s", sa.Integer(), nullable=True),
        sa.Column("doc_anchor", sa.String(length=200), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("agenda_id", "order", name="agenda_items_agenda_order_unique"),
        schema=schema,
    )
    op.create_index(
        "ix_agenda_items_agenda_order",
        _ITEMS_TABLE,
        ["agenda_id", "order"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_agenda_items_agenda_timecode",
        _ITEMS_TABLE,
        ["agenda_id", "video_timecode_s"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_index("ix_agenda_items_agenda_timecode", table_name=_ITEMS_TABLE, schema=schema)
    op.drop_index("ix_agenda_items_agenda_order", table_name=_ITEMS_TABLE, schema=schema)
    op.drop_table(_ITEMS_TABLE, schema=schema)

    op.drop_index("ix_meeting_agendas_asset", table_name=_AGENDAS_TABLE, schema=schema)
    op.drop_index("ix_meeting_agendas_station", table_name=_AGENDAS_TABLE, schema=schema)
    op.drop_table(_AGENDAS_TABLE, schema=schema)
