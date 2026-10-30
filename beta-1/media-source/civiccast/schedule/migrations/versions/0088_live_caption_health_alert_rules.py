# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Seed live-caption and EAS source-health warning rules.

The live caption processing monitor and EAS source poller already use the
existing alert evaluator and delivery hub. Their missing default rules made
the conditions queryable but left fresh installs without an operator-tunable
destination policy. Seed only absent conditions; an operator-defined rule
already covering either condition remains authoritative.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0088_live_caption_health_alert_rules"
down_revision = "0087_retention_terms"
branch_labels = None
depends_on = None

_SEED_AT = datetime(2026, 10, 9, tzinfo=UTC)
_SEED_BY = "system:migration-0088"
_DEFAULT_RULES = (
    ("live-caption-failure", "warning", 3600, 900),
    ("eas-source-unavailable", "warning", 3600, 900),
)


def upgrade() -> None:
    schema = op.get_context().version_table_schema
    bind = op.get_bind()
    rules = sa.Table("alert_rules", sa.MetaData(), autoload_with=bind, schema=schema)
    for condition, severity, re_alert_after, dedupe_window in _DEFAULT_RULES:
        exists = bind.execute(
            sa.select(rules.c.rule_id).where(rules.c.condition == condition).limit(1)
        ).first()
        if exists is not None:
            continue
        bind.execute(
            rules.insert().values(
                rule_id=f"default:{condition}",
                condition=condition,
                enabled=True,
                severity=severity,
                channel_ids_json="[]",
                dedupe_window_seconds=dedupe_window,
                re_alert_after_seconds=re_alert_after,
                scope_channel_id=None,
                notify_on_resolve=True,
                updated_at=_SEED_AT,
                updated_by=_SEED_BY,
            )
        )


def downgrade() -> None:
    schema = op.get_context().version_table_schema
    rules = sa.Table("alert_rules", sa.MetaData(), autoload_with=op.get_bind(), schema=schema)
    op.get_bind().execute(
        rules.delete().where(
            rules.c.rule_id.in_([f"default:{condition}" for condition, *_ in _DEFAULT_RULES]),
            rules.c.updated_by == _SEED_BY,
        )
    )
