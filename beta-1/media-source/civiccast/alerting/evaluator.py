# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8-3 Alert Evaluator — condition derivation, dedupe, quiet-hours gate.

The evaluator runs after each EgressHealthSample is written. It:
1. Derives active conditions (off-air, encoder-death) from the sample.
2. Calls record_alert_condition to record/bump/resolve events.
3. Decides whether a delivery should fire for each event:
   - First firing: always send (subject to quiet hours).
   - Repeat firing: send only when re_alert_after_seconds has elapsed
     since the last "sent" delivery (notify-on-first-failure contract).
4. Applies quiet-hours gating: critical always sends; warning/info are
   held (status="suppressed", next_attempt_at=window_end).
5. Writes an AlertEventDelivery record for every decision (sent,
   suppressed, or no-channel) so the audit trail is complete.

Delivery calls the injected ``dispatch`` hook — a thin shim in S8-4 that
does the actual SMTP/SMS/webhook send. For tests the hook is a no-op stub.

Server-crash detection (startup path) is handled separately via
``evaluate_server_crash``; it does not run on every health-sample tick.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.alerting.models import (
    AlertChannelDb,
    AlertEventDb,
    AlertEventDeliveryDb,
    AlertRule,
    alert_channel_from_db,
)
from civiccast.alerting.store import (
    get_alert_rules,
    record_alert_condition,
)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

SessionFactory = Callable[[], AbstractContextManager[Session]]
DispatchHook = Callable[[str, str, str, str, str], None]
"""dispatch(event_id, delivery_id, channel_id, condition, label) -> None.

The 4th positional is the alert *condition* (e.g. "off-air"), NOT the
channel kind (email/webhook/sms) — that distinction matters because S8-4's
transport routes by ``AlertChannel.kind`` looked up from ``channel_id``.

S8-4 transport receives the delivery_id so it can update the row to
status="failed" on transport errors (enabling retry/dead-letter).
"""


# ---------------------------------------------------------------------------
# Condition derivation
# ---------------------------------------------------------------------------

# States that mean the channel is definitively off-air (not on a slate,
# not transitioning — operator must act or the automation must recover).
_OFF_AIR_STATES = {"STOPPED", "ERROR"}

# States in which the encoder process is running and sending media.
# encoder-death only makes sense when the encoder SHOULD be producing frames.
_ENCODING_STATES = {"ON_AIR"}


def derive_channel_conditions(
    channel_id: str,
    state: str,
    *,
    encoder_fps: float | None,
    encoder_bitrate_kbps: float | None,
) -> list[tuple[str, str]]:
    """Return (kind, summary) pairs for conditions that are currently active.

    QA-004 regression invariant: encoder-death is only raised when
    ``state == ON_AIR`` — a FALLBACK_SLATE channel with fps=0 is expected
    (it is not sending live media) and MUST NOT trigger encoder-death.
    """
    conditions: list[tuple[str, str]] = []

    if state in _OFF_AIR_STATES:
        conditions.append(
            (
                "off-air",
                f"{channel_id} is off air (state={state})",
            )
        )

    if state in _ENCODING_STATES:
        fps_stalled = encoder_fps is not None and encoder_fps == 0
        bitrate_stalled = encoder_bitrate_kbps is not None and encoder_bitrate_kbps == 0
        if fps_stalled or bitrate_stalled:
            conditions.append(
                (
                    "encoder-death",
                    f"{channel_id} encoder stalled (fps={encoder_fps}, bitrate={encoder_bitrate_kbps})",
                )
            )

    return conditions


# ---------------------------------------------------------------------------
# Quiet-hours check
# ---------------------------------------------------------------------------


def _in_quiet_window(
    hour_minute_now: tuple[int, int],
    start: str,
    end: str,
) -> bool:
    """True when *hour_minute_now* (h, m) falls inside the [start, end) window.

    Handles overnight windows (e.g. 22:00-07:00).
    """
    h_now, m_now = hour_minute_now
    now_mins = h_now * 60 + m_now

    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    start_mins = sh * 60 + sm
    end_mins = eh * 60 + em

    if start_mins <= end_mins:
        # Same-day window (e.g. 08:00-18:00)
        return start_mins <= now_mins < end_mins
    # Overnight window (e.g. 22:00-07:00)
    return now_mins >= start_mins or now_mins < end_mins


def _window_end_dt(now: datetime, end: str) -> datetime:
    """Return the datetime when the quiet window closes on the same (or next) day."""
    eh, em = (int(x) for x in end.split(":"))
    candidate = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


# ---------------------------------------------------------------------------
# No-channel sentinel (C1 fix)
# ---------------------------------------------------------------------------

# A rule can legitimately have zero live channels wired to it — a fresh
# install seeds every §6.2 default rule with ``channel_ids: []`` because it
# cannot fabricate operator credentials (SMTP/SMS/webhook secrets). That is
# expected, NOT a bug. What is a bug (C1) is treating the gap as *nothing
# happened*: spec §6.2 requires "a rule with no live channel of its default
# kind logs a suppressed delivery so the gap is visible ... never a silent
# drop." This sentinel channel_id marks that delivery row as a
# no-live-channel gap rather than a real per-channel attempt.
_NO_CHANNEL_SENTINEL_ID = "unconfigured"
# Every §6.2 default-rule row recommends email as (part of) its default
# channel kind, so "email" is the least-surprising placeholder kind for a
# delivery row that, by definition, has no real channel to report a kind
# for. ``AlertEventDelivery.kind`` is a closed Literal (email/sms/webhook)
# so the sentinel must be one of them to round-trip through the pydantic
# conversion the read APIs use.
_NO_CHANNEL_SENTINEL_KIND = "email"


def _no_channel_error(condition: str) -> str:
    return (
        f"No enabled alert channel is configured for condition '{condition}'. "
        "This alert could not be pushed to an operator; configure an email, "
        "SMS, or webhook alert channel in Alert Settings to receive it."
    )


# ---------------------------------------------------------------------------
# Delivery gating logic
# ---------------------------------------------------------------------------


def _last_sent_at(session: Session, event_id: str) -> datetime | None:
    """Return dispatched_at of the most recent 'sent' delivery for *event_id*."""
    row = session.execute(
        select(AlertEventDeliveryDb)
        .where(
            AlertEventDeliveryDb.event_id == event_id,
            AlertEventDeliveryDb.status == "sent",
        )
        .order_by(AlertEventDeliveryDb.dispatched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    dt = row.dispatched_at
    # SQLite returns naive datetimes; normalize to UTC so elapsed calc works.
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _should_send(
    session: Session,
    event_db: AlertEventDb,
    rule: AlertRule,
    now: datetime,
) -> bool:
    """True when a fresh delivery should be dispatched for *event_db*.

    First occurrence always sends. Subsequent occurrences send only when
    re_alert_after_seconds > 0 and enough time has elapsed since the last
    'sent' delivery (the notify-on-first-failure / rate-limit contract).
    re_alert_after_seconds == 0 means one-shot only (server-crash, missing-media).
    """
    if event_db.occurrence_count == 1:
        return True
    if rule.re_alert_after_seconds == 0:
        return False
    last = _last_sent_at(session, event_db.event_id)
    if last is None:
        return True
    elapsed = (now - last).total_seconds()
    return elapsed >= rule.re_alert_after_seconds


def _last_delivery_at_for_channel(
    session: Session, event_id: str, channel_id: str
) -> datetime | None:
    """Return dispatched_at of the most recent delivery row for *event_id* on
    *channel_id*, regardless of status.

    Used only to rate-limit the C1 no-channel-gap delivery: ``_last_sent_at``
    deliberately only counts ``status="sent"`` rows (the real-channel
    dispatch contract), which a no-channel gap can never produce — anchoring
    on that would make ``_should_send`` return True on every occurrence
    (since no "sent" delivery ever exists), writing a fresh suppressed row on
    every health-sample tick instead of once per re_alert_after_seconds
    window like a real send.
    """
    row = session.execute(
        select(AlertEventDeliveryDb)
        .where(
            AlertEventDeliveryDb.event_id == event_id,
            AlertEventDeliveryDb.alert_channel_id == channel_id,
        )
        .order_by(AlertEventDeliveryDb.dispatched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    dt = row.dispatched_at
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _should_log_no_channel_gap(
    session: Session,
    event_db: AlertEventDb,
    rule: AlertRule,
    now: datetime,
) -> bool:
    """Same notify-on-first-failure cadence as ``_should_send``, anchored on
    the no-channel sentinel delivery: log the gap once, then again only after
    ``re_alert_after_seconds`` (never on every tick), matching a one-shot
    rule's single-log contract when ``re_alert_after_seconds == 0``.
    """
    last = _last_delivery_at_for_channel(session, event_db.event_id, _NO_CHANNEL_SENTINEL_ID)
    if last is None:
        return True
    if rule.re_alert_after_seconds == 0:
        return False
    elapsed = (now - last).total_seconds()
    return elapsed >= rule.re_alert_after_seconds


# ---------------------------------------------------------------------------
# AlertEvaluator
# ---------------------------------------------------------------------------


class AlertEvaluator:
    """Evaluates alert conditions after each health-sample tick.

    Injected into the egress daemon as an optional hook. Each call to
    ``evaluate_channel`` opens its own session (like the egress store).

    ``dispatch`` is called when a delivery should fire; it receives
    (event_id, delivery_id, alert_channel_id, condition, label). S8-4 wires
    the actual transport (SMTP/SMS/webhook) behind this hook. In tests it is
    a no-op stub.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        dispatch: DispatchHook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._dispatch = dispatch or (lambda *_: None)

    def evaluate_channel(
        self,
        channel_id: str,
        state: str,
        *,
        encoder_fps: float | None = None,
        encoder_bitrate_kbps: float | None = None,
        additional_conditions: Sequence[tuple[str, str]] = (),
        now: datetime | None = None,
    ) -> None:
        """Derive conditions from the current channel state and manage alert lifecycle."""
        now = now or datetime.now(tz=UTC)
        active_conditions = derive_channel_conditions(
            channel_id,
            state,
            encoder_fps=encoder_fps,
            encoder_bitrate_kbps=encoder_bitrate_kbps,
        )
        active_conditions.extend(additional_conditions)
        active_kinds = {kind for kind, _ in active_conditions}
        dispatches: list[tuple[str, str, str, str, str]] = []

        with self._session_factory() as session:
            # --- Fire active conditions ---
            for kind, summary in active_conditions:
                event = record_alert_condition(
                    session,
                    kind=kind,  # type: ignore[arg-type]
                    resource_ref=channel_id,
                    source_section="S8",
                    summary=summary,
                    observed_at=now,
                )
                if event.state == "firing":
                    self._try_dispatch(session, event.event_id, now, dispatches)

            # --- Resolve conditions that are no longer active ---
            firing_rows = (
                session.execute(
                    select(AlertEventDb).where(
                        AlertEventDb.resource_ref == channel_id,
                        AlertEventDb.state == "firing",
                    )
                )
                .scalars()
                .all()
            )

            for row in firing_rows:
                if row.condition not in active_kinds:
                    resolved = record_alert_condition(
                        session,
                        kind=row.condition,  # type: ignore[arg-type]
                        resource_ref=channel_id,
                        source_section="S8",
                        summary=f"{channel_id} recovered from {row.condition}",
                        observed_at=now,
                        resolved=True,
                    )
                    if resolved.state == "resolved":
                        self._try_dispatch_resolve(session, resolved.event_id, now, dispatches)

            session.commit()

        # The transport opens an independent session. Publish only after the
        # event, its current state, and the delivery are committed and visible.
        for args in dispatches:
            self._dispatch(*args)

    def evaluate_server_crash(
        self,
        channel_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        """Fire a one-shot server-crash alert (called at daemon startup if marker absent)."""
        now = now or datetime.now(tz=UTC)
        dispatches: list[tuple[str, str, str, str, str]] = []
        with self._session_factory() as session:
            event = record_alert_condition(
                session,
                kind="server-crash",
                resource_ref=channel_id,
                source_section="S8",
                summary=f"{channel_id}: CivicCast restarted unexpectedly; channels are recovering",
                observed_at=now,
            )
            if event.state == "firing" and event.occurrence_count == 1:
                self._try_dispatch(session, event.event_id, now, dispatches)
            session.commit()
        for args in dispatches:
            self._dispatch(*args)

    # ------------------------------------------------------------------
    # Delivery helpers
    # ------------------------------------------------------------------

    def _try_dispatch(
        self,
        session: Session,
        event_id: str,
        now: datetime,
        dispatches: list[tuple[str, str, str, str, str]],
    ) -> None:
        """Check all rules + channels for *event_id* and fire delivery if warranted."""
        event_db = session.get(AlertEventDb, event_id)
        if event_db is None:
            return

        # Find matching rule (same logic as record_alert_condition used).
        rules = get_alert_rules(session)
        matched_rule: AlertRule | None = None
        for rule in rules:
            if (
                rule.condition == event_db.condition
                and rule.enabled
                and rule.rule_id == event_db.rule_id
            ):
                matched_rule = rule
                break
        if matched_rule is None:
            return

        if not _should_send(session, event_db, matched_rule, now):
            return

        # Dispatch to each wired, enabled channel.
        channels: Sequence[AlertChannelDb] = []
        if matched_rule.channel_ids:
            channels = (
                session.execute(
                    select(AlertChannelDb).where(
                        AlertChannelDb.channel_id.in_(matched_rule.channel_ids),
                        AlertChannelDb.enabled == True,  # noqa: E712
                    )
                )
                .scalars()
                .all()
            )

        if not channels:
            # C1 fix: a rule with no live channel (empty channel_ids, or every
            # wired channel_id missing/disabled) must NEVER be a silent drop
            # (spec §6.2). Record a suppressed delivery so the gap is visible
            # via the deliveries drawer / support bundle instead of vanishing
            # without a trace — this is what let a fresh install "never call
            # for help" undetectably. Gated on the same notify-on-first-failure
            # cadence as a real send (§6.3) so a persistent gap logs once, then
            # again only after re_alert_after_seconds — not once per tick.
            if _should_log_no_channel_gap(session, event_db, matched_rule, now):
                self._write_delivery(
                    session,
                    event_db.event_id,
                    _NO_CHANNEL_SENTINEL_ID,
                    _NO_CHANNEL_SENTINEL_KIND,
                    "suppressed",
                    now,
                    None,
                    last_error=_no_channel_error(event_db.condition),
                )
            return

        for ch_db in channels:
            ch = alert_channel_from_db(ch_db)
            status, next_attempt_at = self._gate_quiet_hours(
                ch.quiet_hours_start_utc,
                ch.quiet_hours_end_utc,
                event_db.severity,
                now,
            )
            delivery_id = self._write_delivery(
                session, event_db.event_id, ch.channel_id, ch.kind, status, now, next_attempt_at
            )
            if status == "sent":
                dispatches.append(
                    (event_db.event_id, delivery_id, ch.channel_id, event_db.condition, ch.label)
                )

    def _try_dispatch_resolve(
        self,
        session: Session,
        event_id: str,
        now: datetime,
        dispatches: list[tuple[str, str, str, str, str]],
    ) -> None:
        """Dispatch resolve notification if the rule says notify_on_resolve."""
        event_db = session.get(AlertEventDb, event_id)
        if event_db is None:
            return

        rules = get_alert_rules(session)
        matched_rule: AlertRule | None = None
        for rule in rules:
            if (
                rule.condition == event_db.condition
                and rule.enabled
                and rule.rule_id == event_db.rule_id
            ):
                matched_rule = rule
                break
        if matched_rule is None or not matched_rule.notify_on_resolve:
            return

        channels: Sequence[AlertChannelDb] = []
        if matched_rule.channel_ids:
            channels = (
                session.execute(
                    select(AlertChannelDb).where(
                        AlertChannelDb.channel_id.in_(matched_rule.channel_ids),
                        AlertChannelDb.enabled == True,  # noqa: E712
                    )
                )
                .scalars()
                .all()
            )

        if not channels:
            # C1 fix: same no-silent-drop contract applies to the resolve
            # notification — the operator never learns the channel recovered
            # either, and that gap must be visible too.
            self._write_delivery(
                session,
                event_db.event_id,
                _NO_CHANNEL_SENTINEL_ID,
                _NO_CHANNEL_SENTINEL_KIND,
                "suppressed",
                now,
                None,
                last_error=_no_channel_error(event_db.condition),
            )
            return

        for ch_db in channels:
            ch = alert_channel_from_db(ch_db)
            delivery_id = self._write_delivery(
                session, event_db.event_id, ch.channel_id, ch.kind, "sent", now, None
            )
            dispatches.append(
                (event_db.event_id, delivery_id, ch.channel_id, event_db.condition, ch.label)
            )

    def _gate_quiet_hours(
        self,
        start: str | None,
        end: str | None,
        severity: str,
        now: datetime,
    ) -> tuple[str, datetime | None]:
        """Return (status, next_attempt_at). Critical bypasses quiet hours."""
        if severity == "critical" or start is None or end is None:
            return ("sent", None)
        h, m = now.hour, now.minute
        if _in_quiet_window((h, m), start, end):
            return ("suppressed", _window_end_dt(now, end))
        return ("sent", None)

    def _write_delivery(
        self,
        session: Session,
        event_id: str,
        channel_id: str,
        kind: str,
        status: str,
        now: datetime,
        next_attempt_at: datetime | None,
        last_error: str = "",
    ) -> str:
        delivery_id = f"del-{uuid.uuid4().hex}"
        row = AlertEventDeliveryDb(
            delivery_id=delivery_id,
            event_id=event_id,
            alert_channel_id=channel_id,
            kind=kind,
            status=status,
            attempts=1 if status == "sent" else 0,
            next_attempt_at=next_attempt_at,
            last_error=last_error,
            dispatched_at=now,
        )
        session.add(row)
        session.flush()
        return delivery_id
