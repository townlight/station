# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Add ``agenda_items.confidence`` for heuristic/AI-assisted imports.

Product hole fix: ``AgendaService.import_from_doc`` previously raised
``NotImplementedError`` for any non-``text/plain`` upload (i.e. every PDF
agenda). This chain adds real PDF parsing (``civiccast.agenda.pdf_import``,
a heuristic text-layer extractor) that scores each recognized line's
reliability. ``confidence`` (nullable float, 0.0-1.0) carries that score so
the operator console can flag low-confidence rows before the operator
publishes -- it is always ``NULL`` for operator-authored items and exact
plain-text imports, since there is nothing to be uncertain about there.

Chain HEAD was ``0075_offline_caption_jobs`` (captions module) when this
migration was first written as ``0076_agenda_item_confidence`` -- confirmed
via ``alembic heads`` (single head) at that time. Renumbered to ``0078``
(down_revision moved to ``0076_analytics_viewership``) after PR #20 merged
first and independently claimed the ``0076`` slot for the S14 analytics
module (``civiccast/analytics/migrations/versions/0076_analytics_viewership.py``).
PR #19 (``feat/s7-media-lifecycle``) has reserved the ``0077`` slot but had
not merged into ``main`` as of this rename -- ``0077`` is deliberately left
unused here so this migration does not collide with it too when it lands;
a future merge/renumber may be needed at that point the same way the
``0058_meeting_agenda`` docstring describes for the historical ``0056``
reserved slot. This is the current continuation of the shared cross-module
numbered-slice chain.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078_agenda_item_confidence"
down_revision = "0076_analytics_viewership"
branch_labels = None
depends_on = None

_ITEMS_TABLE = "agenda_items"


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    op.add_column(
        _ITEMS_TABLE,
        sa.Column("confidence", sa.Float(), nullable=True),
        schema=schema,
    )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    op.drop_column(_ITEMS_TABLE, "confidence", schema=schema)
