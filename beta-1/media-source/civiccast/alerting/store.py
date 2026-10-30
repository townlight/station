# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8 alerting store — CRUD and the ``record_alert_condition`` hub.

All sections call ``record_alert_condition`` to route operational conditions
to the alerting hub. S8 owns dedupe (notify-on-first-failure), occurrence
counting, and resolve lifecycle. Delivery dispatch is S8-3/S8-4 (evaluator
and delivery stack).

Database access takes an explicit ``Session`` parameter, matching the
pattern of ``installer/service.py`` service functions — callers supply the
session from FastAPI dependency injection or from the daemon's session
factory.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.alerting.models import (
    AlertChannel,
    AlertChannelDb,
    AlertConditionKind,
    AlertEvent,
    AlertEventDb,
    AlertEventDelivery,
    AlertEventDeliveryDb,
    AlertRule,
    AlertRuleDb,
    SystemResourceSample,
    SystemResourceSampleDb,
    SystemSelfTest,
    SystemSelfTestDb,
    alert_channel_from_db,
    alert_event_delivery_from_db,
    alert_event_from_db,
    alert_rule_from_db,
    system_resource_sample_from_db,
    system_self_test_from_db,
)

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# AlertRule CRUD
# ---------------------------------------------------------------------------


def get_alert_rules(session: Session) -> list[AlertRule]:
    rows = session.execute(select(AlertRuleDb).order_by(AlertRuleDb.condition)).scalars().all()
    return [alert_rule_from_db(r) for r in rows]


def get_alert_rule(session: Session, rule_id: str) -> AlertRule | None:
    row = session.get(AlertRuleDb, rule_id)
    return alert_rule_from_db(row) if row else None


def upsert_alert_rule(session: Session, rule: AlertRule) -> AlertRule:
    existing = session.get(AlertRuleDb, rule.rule_id)
    if existing is None:
        row = AlertRuleDb(
            rule_id=rule.rule_id,
            condition=rule.condition,
            enabled=rule.enabled,
            severity=rule.severity,
            channel_ids_json=json.dumps(rule.channel_ids),
            dedupe_window_seconds=rule.dedupe_window_seconds,
            re_alert_after_seconds=rule.re_alert_after_seconds,
            scope_channel_id=rule.scope_channel_id,
            notify_on_resolve=rule.notify_on_resolve,
            updated_at=rule.updated_at,
            updated_by=rule.updated_by,
        )
        session.add(row)
    else:
        existing.condition = rule.condition
        existing.enabled = rule.enabled
        existing.severity = rule.severity
        existing.channel_ids_json = json.dumps(rule.channel_ids)
        existing.dedupe_window_seconds = rule.dedupe_window_seconds
        existing.re_alert_after_seconds = rule.re_alert_after_seconds
        existing.scope_channel_id = rule.scope_channel_id
        existing.notify_on_resolve = rule.notify_on_resolve
        existing.updated_at = rule.updated_at
        existing.updated_by = rule.updated_by
    session.flush()
    return rule


# ---------------------------------------------------------------------------
# AlertChannel CRUD
# ---------------------------------------------------------------------------


def get_alert_channels(session: Session) -> list[AlertChannel]:
    rows = session.execute(select(AlertChannelDb).order_by(AlertChannelDb.label)).scalars().all()
    return [alert_channel_from_db(r) for r in rows]


def get_alert_channel(session: Session, channel_id: str) -> AlertChannel | None:
    row = session.get(AlertChannelDb, channel_id)
    return alert_channel_from_db(row) if row else None


def upsert_alert_channel(session: Session, channel: AlertChannel) -> AlertChannel:
    existing = session.get(AlertChannelDb, channel.channel_id)
    if existing is None:
        row = AlertChannelDb(
            channel_id=channel.channel_id,
            kind=channel.kind,
            label=channel.label,
            enabled=channel.enabled,
            target_redacted=channel.target_redacted,
            credential_handle=channel.credential_handle,
            quiet_hours_start_utc=channel.quiet_hours_start_utc,
            quiet_hours_end_utc=channel.quiet_hours_end_utc,
            last_delivery_status=channel.last_delivery_status,
            last_delivery_at=channel.last_delivery_at,
            created_at=channel.created_at,
        )
        session.add(row)
    else:
        existing.kind = channel.kind
        existing.label = channel.label
        existing.enabled = channel.enabled
        existing.target_redacted = channel.target_redacted
        existing.credential_handle = channel.credential_handle
        existing.quiet_hours_start_utc = channel.quiet_hours_start_utc
        existing.quiet_hours_end_utc = channel.quiet_hours_end_utc
        existing.last_delivery_status = channel.last_delivery_status
        existing.last_delivery_at = channel.last_delivery_at
    session.flush()
    return channel


def delete_alert_channel(session: Session, channel_id: str) -> bool:
    row = session.get(AlertChannelDb, channel_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


# ---------------------------------------------------------------------------
# AlertEvent CRUD
# ---------------------------------------------------------------------------


def get_alert_events(
    session: Session,
    *,
    state: str | None = None,
    severity: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[AlertEvent]:
    stmt = select(AlertEventDb).order_by(AlertEventDb.last_observed_at.desc())
    if state is not None:
        stmt = stmt.where(AlertEventDb.state == state)
    if severity is not None:
        stmt = stmt.where(AlertEventDb.severity == severity)
    if since is not None:
        stmt = stmt.where(AlertEventDb.first_observed_at >= since)
    stmt = stmt.limit(limit)
    return [alert_event_from_db(r) for r in session.execute(stmt).scalars().all()]


def get_alert_event(session: Session, event_id: str) -> AlertEvent | None:
    row = session.get(AlertEventDb, event_id)
    return alert_event_from_db(row) if row else None


def acknowledge_alert_event(
    session: Session, event_id: str, *, by: str, at: datetime
) -> AlertEvent | None:
    row = session.get(AlertEventDb, event_id)
    if row is None:
        return None
    row.acknowledged_at = at
    row.acknowledged_by = by
    session.flush()
    return alert_event_from_db(row)


# ---------------------------------------------------------------------------
# AlertEventDelivery CRUD
# ---------------------------------------------------------------------------


def get_event_deliveries(session: Session, event_id: str) -> list[AlertEventDelivery]:
    rows = (
        session.execute(
            select(AlertEventDeliveryDb)
            .where(AlertEventDeliveryDb.event_id == event_id)
            .order_by(AlertEventDeliveryDb.dispatched_at.desc())
        )
        .scalars()
        .all()
    )
    return [alert_event_delivery_from_db(r) for r in rows]


def upsert_event_delivery(session: Session, delivery: AlertEventDelivery) -> AlertEventDelivery:
    existing = session.get(AlertEventDeliveryDb, delivery.delivery_id)
    if existing is None:
        row = AlertEventDeliveryDb(
            delivery_id=delivery.delivery_id,
            event_id=delivery.event_id,
            alert_channel_id=delivery.alert_channel_id,
            kind=delivery.kind,
            status=delivery.status,
            attempts=delivery.attempts,
            next_attempt_at=delivery.next_attempt_at,
            last_error=delivery.last_error,
            signature=delivery.signature,
            dispatched_at=delivery.dispatched_at,
        )
        session.add(row)
    else:
        existing.status = delivery.status
        existing.attempts = delivery.attempts
        existing.next_attempt_at = delivery.next_attempt_at
        existing.last_error = delivery.last_error
        existing.signature = delivery.signature
    session.flush()
    return delivery


# ---------------------------------------------------------------------------
# SystemResourceSample CRUD
# ---------------------------------------------------------------------------


def append_resource_sample(session: Session, sample: SystemResourceSample) -> SystemResourceSample:
    row = SystemResourceSampleDb(
        sampled_at=sample.sampled_at,
        cpu_percent=sample.cpu_percent,
        ram_used_gb=sample.ram_used_gb,
        ram_total_gb=sample.ram_total_gb,
        gpu_percent=sample.gpu_percent,
        gpu_vram_used_gb=sample.gpu_vram_used_gb,
        media_volume_free_gb=sample.media_volume_free_gb,
        backup_volume_free_gb=sample.backup_volume_free_gb,
        db_reachable=sample.db_reachable,
        backup_volume_writable=sample.backup_volume_writable,
        service_running=sample.service_running,
        clock_skew_seconds=sample.clock_skew_seconds,
    )
    session.add(row)
    session.flush()
    return system_resource_sample_from_db(row)


def recent_resource_samples(
    session: Session,
    *,
    window_minutes: int = 60,
    limit: int = 120,
    now: datetime | None = None,
) -> list[SystemResourceSample]:
    from datetime import timedelta

    reference = now if now is not None else datetime.now(tz=UTC)
    cutoff = reference - timedelta(minutes=window_minutes)
    rows = (
        session.execute(
            select(SystemResourceSampleDb)
            .where(SystemResourceSampleDb.sampled_at >= cutoff)
            .order_by(SystemResourceSampleDb.sampled_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [system_resource_sample_from_db(r) for r in rows]


# ---------------------------------------------------------------------------
# SystemSelfTest CRUD
# ---------------------------------------------------------------------------


def upsert_self_test(session: Session, test: SystemSelfTest) -> SystemSelfTest:
    existing = session.get(SystemSelfTestDb, test.self_test_id)
    if existing is None:
        row = SystemSelfTestDb(
            self_test_id=test.self_test_id,
            kind=test.kind,
            started_at=test.started_at,
            finished_at=test.finished_at,
            status=test.status,
            checks_json=json.dumps(test.checks),
            summary=test.summary,
            evidence_path=test.evidence_path,
        )
        session.add(row)
    else:
        existing.finished_at = test.finished_at
        existing.status = test.status
        existing.checks_json = json.dumps(test.checks)
        existing.summary = test.summary
        existing.evidence_path = test.evidence_path
    session.flush()
    return test


def get_self_tests(
    session: Session,
    *,
    kind: str | None = None,
    since: datetime | None = None,
    limit: int = 30,
) -> list[SystemSelfTest]:
    stmt = select(SystemSelfTestDb).order_by(SystemSelfTestDb.started_at.desc())
    if kind is not None:
        stmt = stmt.where(SystemSelfTestDb.kind == kind)
    if since is not None:
        stmt = stmt.where(SystemSelfTestDb.started_at >= since)
    stmt = stmt.limit(limit)
    return [system_self_test_from_db(r) for r in session.execute(stmt).scalars().all()]


# ---------------------------------------------------------------------------
# AlertEventDelivery retry helpers
# ---------------------------------------------------------------------------


def fail_delivery(
    session: Session,
    delivery_id: str,
    error: str,
    next_attempt_at: datetime,
) -> None:
    """Mark a delivery as failed and schedule the next retry attempt."""
    row = session.get(AlertEventDeliveryDb, delivery_id)
    if row is None:
        return
    row.status = "failed"
    row.last_error = error[:1000]
    row.attempts += 1
    row.next_attempt_at = next_attempt_at
    session.flush()


def succeed_delivery(session: Session, delivery_id: str) -> None:
    """Mark a retried delivery as successfully sent."""
    row = session.get(AlertEventDeliveryDb, delivery_id)
    if row is None:
        return
    row.status = "sent"
    row.next_attempt_at = None
    session.flush()


def dead_letter_delivery(session: Session, delivery_id: str) -> None:
    """Move a delivery to dead-letter state; clears next_attempt_at."""
    row = session.get(AlertEventDeliveryDb, delivery_id)
    if row is None:
        return
    row.status = "dead_letter"
    row.next_attempt_at = None
    session.flush()


def due_retry_deliveries(
    session: Session, now: datetime, *, limit: int = 100
) -> list[AlertEventDelivery]:
    """Return failed deliveries whose retry window has elapsed (next_attempt_at <= now)."""
    rows = (
        session.execute(
            select(AlertEventDeliveryDb)
            .where(
                AlertEventDeliveryDb.status == "failed",
                AlertEventDeliveryDb.next_attempt_at <= now,
            )
            .order_by(AlertEventDeliveryDb.next_attempt_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [alert_event_delivery_from_db(r) for r in rows]


# ---------------------------------------------------------------------------
# record_alert_condition — the cross-section condition-ingest hub
# ---------------------------------------------------------------------------


def record_alert_condition(
    session: Session,
    *,
    kind: AlertConditionKind,
    resource_ref: str,
    source_section: str,
    summary: str,
    detail: str = "",
    observed_at: datetime | None = None,
    resolved: bool = False,
) -> AlertEvent:
    """Ingest an operational condition from any section into the alerting hub.

    This is the ONLY way other sections raise an operational condition. S9's
    restart-escalation proof event is the first upstream producer. S8 self-derives
    off-air/encoder-death/server-crash; all other conditions arrive here.

    Dedupe contract (notify-on-first-failure):
    - First call for a (kind, resource_ref) pair with resolved=False: create a
      new ``AlertEvent(state="firing")`` and return it. The caller/evaluator
      (S8-3) dispatches delivery for the matched rules.
    - Subsequent calls with the same pair: bump ``occurrence_count`` and update
      ``last_observed_at``. No new event; the existing event is returned. The
      evaluator checks ``re_alert_after_seconds`` for re-alert decisions (S8-3).
    - Call with resolved=True: find the firing event and set state="resolved".
      The evaluator dispatches a resolve notification if notify_on_resolve (S8-3).

    Delivery dispatch is intentionally NOT done here (that is S8-3/S8-4). S8-2
    provides the pure event-lifecycle store; the evaluator wires the dispatch loop.
    """

    now = observed_at if observed_at is not None else datetime.now(tz=UTC)

    # Find the matching rule — prefer the most specific (scope_channel_id set).
    matched_rule = _find_matching_rule(session, kind, resource_ref)
    rule_id = matched_rule.rule_id if matched_rule else ""
    severity = matched_rule.severity if matched_rule else "warning"

    # Find any existing firing event for this dedupe_key.
    firing_row = _find_firing_event(session, kind, resource_ref)

    if resolved:
        if firing_row is not None:
            firing_row.state = "resolved"
            firing_row.resolved_at = now
            firing_row.last_observed_at = now
            session.flush()
            return alert_event_from_db(firing_row)
        # Nothing to resolve — create a pre-resolved event for the audit trail.
        return _create_event(
            session,
            kind=kind,
            resource_ref=resource_ref,
            rule_id=rule_id,
            severity=severity,
            source_section=source_section,
            summary=summary,
            detail=detail,
            now=now,
            state="resolved",
        )

    if firing_row is not None:
        # Already firing: bump count, update last_observed_at.
        firing_row.occurrence_count += 1
        firing_row.last_observed_at = now
        # Summary and detail can evolve as the condition persists.
        firing_row.summary = summary[:300]
        firing_row.detail = detail[:2000]
        session.flush()
        return alert_event_from_db(firing_row)

    # No existing firing event: create one.
    return _create_event(
        session,
        kind=kind,
        resource_ref=resource_ref,
        rule_id=rule_id,
        severity=severity,
        source_section=source_section,
        summary=summary,
        detail=detail,
        now=now,
        state="firing",
    )


def _find_matching_rule(session: Session, kind: str, resource_ref: str) -> AlertRule | None:
    """Return the best-matching enabled rule: prefer scoped, fall back to wildcard."""
    rows = (
        session.execute(
            select(AlertRuleDb).where(
                AlertRuleDb.condition == kind,
                AlertRuleDb.enabled == True,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    # Prefer scoped rules whose scope matches the resource_ref prefix.
    for row in rows:
        if row.scope_channel_id and resource_ref.startswith(row.scope_channel_id):
            return alert_rule_from_db(row)
    # Fall back to wildcard rules.
    for row in rows:
        if row.scope_channel_id is None:
            return alert_rule_from_db(row)
    return None


def _find_firing_event(session: Session, kind: str, resource_ref: str) -> AlertEventDb | None:
    return session.execute(
        select(AlertEventDb).where(
            AlertEventDb.condition == kind,
            AlertEventDb.resource_ref == resource_ref,
            AlertEventDb.state == "firing",
        )
    ).scalar_one_or_none()


def _create_event(
    session: Session,
    *,
    kind: str,
    resource_ref: str,
    rule_id: str,
    severity: str,
    source_section: str,
    summary: str,
    detail: str,
    now: datetime,
    state: str,
) -> AlertEvent:
    event_id = f"alert-{uuid.uuid4().hex}"
    row = AlertEventDb(
        event_id=event_id,
        rule_id=rule_id,
        condition=kind,
        severity=severity,
        state=state,
        resource_ref=resource_ref,
        summary=summary[:300],
        detail=detail[:2000],
        source_section=source_section[:8],
        first_observed_at=now,
        last_observed_at=now,
        resolved_at=now if state == "resolved" else None,
        occurrence_count=1,
    )
    session.add(row)
    session.flush()
    return alert_event_from_db(row)
