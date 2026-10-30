# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persist observed readiness on every configured live source.

Revision ID: 0086_live_source_probe_state
Revises: 0083_caption_review_language
Create Date: 2026-09-02

WP-07 / audit ENG-003. ``live_sources`` rows used to be treated as ``ready``
because they existed: ``civiccast.live.relay._source_path`` stamped
``health_state='ready'`` on every configured row, and that value is the only
gate ``civiccast.egress.live_takeover.build_live_takeover_source_plan``
applies before a manual takeover changes what is on air. These columns make
readiness an observation instead of an assumption, and make it survive a
restart -- an in-memory cache would have reset every source to "ready" on the
first request after a service restart, which is exactly the wrong default
thirty seconds before a meeting gavels in.

Columns:

``probe_state``           never_probed | ready | failed (CHECK-constrained).
``probe_observed_at``     when the last probe ran.
``probe_detail``          operator-safe, truncated, secret-redacted reason.
``probe_error_code``      stable machine code for the last outcome.
``probe_last_success_at`` last time this source actually delivered media.
``row_version``           optimistic-concurrency token for the new PATCH path.

Backfill: every existing row lands on ``probe_state='never_probed'`` with null
timestamps and ``row_version=1`` -- carried by the server defaults, and
asserted explicitly by ``upgrade()`` for the rows that predate the defaults on
backends where an added NOT NULL column's default is not written back. That is
the deliberately conservative direction: an already-configured source becomes
"nobody has looked yet" rather than inheriting a readiness claim nothing ever
verified.

Revision numbers are repo-global. At authoring time ``alembic heads`` reported
one head, ``0082_egress_graphics_overlay``; the integration plan sequenced
``0083_caption_review_language`` / ``0084_podcast_publish_jobs`` /
``0085_notification_delivery_outcomes`` ahead of this revision, pending on
which of them landed to `main` first. WP-05 (0085) is parked by owner
decision and will not land. ``#131`` merged ``0083_caption_review_language``
to ``main`` (0084 never materialized), so this revision parents on
``0083_caption_review_language`` -- the sole other head at merge time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0086_live_source_probe_state"
down_revision = "0083_caption_review_language"
branch_labels = None
depends_on = None

_TABLE = "live_sources"
_CHECK_NAME = "live_sources_probe_state_check"


def _use_schema() -> bool:
    return op.get_bind().dialect.name != "sqlite"


def upgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    op.add_column(
        _TABLE,
        sa.Column(
            "probe_state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'never_probed'"),
        ),
        schema=schema,
    )
    op.add_column(
        _TABLE,
        sa.Column("probe_observed_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.add_column(_TABLE, sa.Column("probe_detail", sa.Text(), nullable=True), schema=schema)
    op.add_column(
        _TABLE,
        sa.Column("probe_error_code", sa.String(length=48), nullable=True),
        schema=schema,
    )
    op.add_column(
        _TABLE,
        sa.Column("probe_last_success_at", sa.DateTime(timezone=True), nullable=True),
        schema=schema,
    )
    op.add_column(
        _TABLE,
        sa.Column("row_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        schema=schema,
    )

    # Explicit backfill. The server defaults already cover every backend this
    # product ships on, but stating it makes the intended value of a
    # pre-existing row a tested fact rather than a backend behavior we assume.
    qualified = f"{schema}.{_TABLE}" if schema else _TABLE
    op.execute(
        sa.text(
            f"UPDATE {qualified} SET probe_state = 'never_probed' "  # noqa: S608 - fixed identifiers  # nosec B608
            "WHERE probe_state IS NULL OR probe_state = ''"
        )
    )
    op.execute(
        sa.text(
            f"UPDATE {qualified} SET row_version = 1 "  # noqa: S608 - fixed identifiers  # nosec B608
            "WHERE row_version IS NULL OR row_version < 1"
        )
    )

    # SQLite cannot ALTER a table to add a CHECK constraint; batch_alter_table
    # rebuilds it. The other backends take the constraint directly.
    if schema is None:
        with op.batch_alter_table(_TABLE) as batch:
            batch.create_check_constraint(
                _CHECK_NAME, "probe_state IN ('never_probed', 'ready', 'failed')"
            )
    else:
        op.create_check_constraint(
            _CHECK_NAME,
            _TABLE,
            "probe_state IN ('never_probed', 'ready', 'failed')",
            schema=schema,
        )


def downgrade() -> None:
    schema = "civiccast" if _use_schema() else None

    if schema is None:
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(_CHECK_NAME, type_="check")
    else:
        op.drop_constraint(_CHECK_NAME, _TABLE, type_="check", schema=schema)

    for column in (
        "row_version",
        "probe_last_success_at",
        "probe_error_code",
        "probe_detail",
        "probe_observed_at",
        "probe_state",
    ):
        op.drop_column(_TABLE, column, schema=schema)
