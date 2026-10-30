# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8-4 alert delivery stack — transport, dispatch hook, and retry worker.

Design (OD-4 / OD-7):
- AlertDeliveryDispatch is the concrete DispatchHook injected into AlertEvaluator.
  After the evaluator commits an optimistic "sent" delivery row, it calls the
  hook outside that transaction to do the actual transport. On failure the hook
  commits "failed" so AlertRetryWorker can reattempt with bounded backoff.
  This is not a transactional outbox: a process crash between that first commit
  and the transport can still leave an optimistic "sent" row without a send.
- Dead-letter after max_attempts surfaces a "service-down" condition so the
  operator sees the persistent failure on the S8 dashboard.
- Credentials (SMTP password, webhook secret) are never stored in the DB; they
  are fetched via a GetCredential callable backed by the local credential store.
"""

from __future__ import annotations

import json
import logging
import smtplib
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.orm import Session

from civiccast.alerting.models import AlertChannel, AlertEvent, AlertEventDelivery
from civiccast.alerting.store import (
    dead_letter_delivery,
    due_retry_deliveries,
    fail_delivery,
    get_alert_channel,
    get_alert_event,
    record_alert_condition,
    succeed_delivery,
)
from civiccast.common.webhook_sign import sign_body

if TYPE_CHECKING:
    pass

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

GetCredential = Callable[[str], dict[str, str] | None]
"""Resolve a credential handle to a dict of secrets; returns None if missing."""

SessionFactory = Callable[[], AbstractContextManager[Session]]
"""Context-manager factory that yields a SQLAlchemy Session."""

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------


@dataclass
class AlertRetrySettings:
    """Bounded exponential backoff for alert delivery retries.

    Backoff sequence (base=120s): 120s, 240s, 480s, 960s, dead-letter.
    """

    max_attempts: int = 5
    base_backoff_seconds: int = 120
    extra: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Concrete senders
# ---------------------------------------------------------------------------


class AlertSmtpSender:
    """Send an alert notification via SMTP with STARTTLS.

    Required credential keys: ``smtp_host``, ``smtp_port`` (default 587),
    ``from_addr``, ``to_addr``. Optional: ``smtp_user``, ``smtp_password``.
    """

    def send(
        self,
        event: AlertEvent,
        channel: AlertChannel,
        get_credential: GetCredential,
    ) -> bool:
        handle = channel.credential_handle
        if handle is None:
            _LOG.warning("smtp channel %s: no credential_handle configured", channel.channel_id)
            return False
        cred = get_credential(handle)
        if not cred:
            _LOG.warning("smtp channel %s: credential %r not found", channel.channel_id, handle)
            return False
        try:
            msg = EmailMessage()
            msg["Subject"] = (
                f"[CivicCast] {event.severity.upper()}: {event.condition} — {event.resource_ref}"
            )
            msg["From"] = cred["from_addr"]
            msg["To"] = cred["to_addr"]
            msg.set_content(
                f"Condition:  {event.condition}\n"
                f"Severity:   {event.severity}\n"
                f"Resource:   {event.resource_ref}\n"
                f"Summary:    {event.summary}\n"
                f"Detail:     {event.detail}\n"
                f"State:      {event.state}\n"
                f"Observed:   {event.last_observed_at.isoformat()}\n"
                f"First seen: {event.first_observed_at.isoformat()}\n"
            )
            host = cred.get("smtp_host", "localhost")
            port = int(cred.get("smtp_port", "587"))
            user = cred.get("smtp_user")
            password = cred.get("smtp_password")
            with smtplib.SMTP(host, port, timeout=10) as smtp:
                smtp.starttls()
                if user and password:
                    smtp.login(user, password)
                smtp.send_message(msg)
            _LOG.info(
                "smtp alert sent for event %s via channel %s", event.event_id, channel.channel_id
            )
            return True
        except Exception as exc:
            _LOG.warning(
                "smtp send failed for channel %s (event %s): %s",
                channel.channel_id,
                event.event_id,
                exc,
            )
            return False


class AlertWebhookSender:
    """Send an alert notification via HTTPS POST with HMAC-SHA-256 signature (OD-7).

    Required credential keys: ``url``, ``secret``.
    The X-CivicCast-Signature header carries the hex HMAC of the JSON body.
    """

    def send(
        self,
        event: AlertEvent,
        channel: AlertChannel,
        get_credential: GetCredential,
    ) -> bool:
        handle = channel.credential_handle
        if handle is None:
            _LOG.warning("webhook channel %s: no credential_handle configured", channel.channel_id)
            return False
        cred = get_credential(handle)
        if not cred:
            _LOG.warning("webhook channel %s: credential %r not found", channel.channel_id, handle)
            return False
        try:
            payload = {
                "event_id": event.event_id,
                "condition": event.condition,
                "severity": event.severity,
                "state": event.state,
                "resource_ref": event.resource_ref,
                "summary": event.summary,
                "detail": event.detail,
                "first_observed_at": event.first_observed_at.isoformat(),
                "last_observed_at": event.last_observed_at.isoformat(),
            }
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            signature = sign_body(body, cred.get("secret", ""))
            with httpx.Client(timeout=10) as client:
                resp = client.post(
                    cred["url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-CivicCast-Signature": signature,
                    },
                )
                resp.raise_for_status()
            _LOG.info(
                "webhook alert sent for event %s via channel %s (status %s)",
                event.event_id,
                channel.channel_id,
                resp.status_code,
            )
            return True
        except Exception as exc:
            _LOG.warning(
                "webhook send failed for channel %s (event %s): %s",
                channel.channel_id,
                event.event_id,
                exc,
            )
            return False


class AlertSmsSender:
    """Send an alert notification via SMS using the Twilio REST API.

    Per-channel credentials (resolved via ``credential_handle`` from the local
    credential store the installer manages) MUST include:

    * ``account_sid``  — Twilio account SID (``AC...``)
    * ``auth_token``   — Twilio auth token
    * ``from_number``  — Twilio-issued phone in E.164 (``+1...``)
    * ``to_number``    — Operator destination phone in E.164

    Optional:

    * ``base_url`` — override Twilio API base (e.g. for a regional or test
      instance). Defaults to ``https://api.twilio.com``.
    * ``messaging_service_sid`` — Twilio Messaging Service SID
      (``MG...``); when set the request uses ``MessagingServiceSid``
      instead of ``From`` so Twilio picks the sender from the pool.

    The body is a short, human-readable summary of the alert (severity,
    condition, resource ref, summary line). Twilio's hard cap is 1600
    characters per segment; we truncate at 320 chars to keep the page
    on the operator's lock screen.

    PII posture: phone numbers are NEVER stored on the channel row — they
    live in the credential store next to the auth token. The row carries
    only ``target_redacted`` (e.g. ``+1 *** *** 4242``) for the operator
    UI display. Same pattern the SMTP sender uses for email addresses.

    Failure cases (non-2xx, network error, missing credential field)
    return ``False`` so the existing retry + dead-letter machinery in
    ``AlertDeliveryDispatch`` handles backoff and eventual surface to the
    ``service-down`` condition.

    Mock-by-default: when no ``credential_handle`` is configured OR the
    credential resolves to ``None``, the sender logs + returns ``False``
    (same as the SMTP + webhook senders). For tests, the dispatch hook
    accepts an explicit ``AlertSmsSender`` instance — a stub that records
    the call without actually contacting Twilio.
    """

    # Twilio default API base. Override via ``credential["base_url"]``
    # for regional endpoints (e.g. ``https://api.us1.twilio.com``).
    _DEFAULT_BASE_URL = "https://api.twilio.com"
    _API_VERSION = "2010-04-01"
    _TIMEOUT_SECONDS = 10.0
    # Twilio splits long messages into 160-char segments (or 70 for non-GSM).
    # We cap our body at 320 so a typical PAGER-style alert fits in 1-2
    # segments without truncation surprises mid-operator-name.
    _BODY_MAX_CHARS = 320

    def send(
        self,
        event: AlertEvent,
        channel: AlertChannel,
        get_credential: GetCredential,
    ) -> bool:
        handle = channel.credential_handle
        if handle is None:
            _LOG.warning("sms channel %s: no credential_handle configured", channel.channel_id)
            return False
        cred = get_credential(handle)
        if not cred:
            _LOG.warning("sms channel %s: credential %r not found", channel.channel_id, handle)
            return False

        account_sid = cred.get("account_sid", "")
        auth_token = cred.get("auth_token", "")
        to_number = cred.get("to_number", "")
        if not (account_sid and auth_token and to_number):
            _LOG.warning(
                "sms channel %s: credential %r missing required fields "
                "(account_sid / auth_token / to_number)",
                channel.channel_id,
                handle,
            )
            return False

        from_number = cred.get("from_number", "")
        messaging_service_sid = cred.get("messaging_service_sid", "")
        if not (from_number or messaging_service_sid):
            _LOG.warning(
                "sms channel %s: credential %r needs either from_number or messaging_service_sid",
                channel.channel_id,
                handle,
            )
            return False

        base_url = cred.get("base_url", self._DEFAULT_BASE_URL).rstrip("/")
        body_text = _format_sms_body(event)

        form_data: dict[str, str] = {"To": to_number, "Body": body_text}
        if messaging_service_sid:
            form_data["MessagingServiceSid"] = messaging_service_sid
        else:
            form_data["From"] = from_number

        url = f"{base_url}/{self._API_VERSION}/Accounts/{account_sid}/Messages.json"
        try:
            with httpx.Client(timeout=self._TIMEOUT_SECONDS) as client:
                resp = client.post(
                    url,
                    data=form_data,
                    auth=(account_sid, auth_token),
                )
                resp.raise_for_status()
        except Exception as exc:
            _LOG.warning(
                "sms send failed for channel %s (event %s): %s",
                channel.channel_id,
                event.event_id,
                exc,
            )
            return False

        # Successful response carries the message SID at JSON path "sid".
        sid_redacted = ""
        try:
            sid_redacted = (resp.json().get("sid") or "")[:8] + "…"
        except Exception:
            sid_redacted = "<unknown-sid>"
        _LOG.info(
            "sms alert sent for event %s via channel %s (twilio_sid=%s)",
            event.event_id,
            channel.channel_id,
            sid_redacted,
        )
        return True


def _format_sms_body(event: AlertEvent) -> str:
    """Build a short, human-readable SMS body for an alert.

    Truncates at ``AlertSmsSender._BODY_MAX_CHARS`` so the page fits in
    1-2 Twilio segments and reads cleanly on a lock screen.
    """
    head = f"CivicCast {event.severity.upper()}: {event.condition}"
    body_parts = [head]
    if event.resource_ref:
        body_parts.append(f"({event.resource_ref})")
    if event.summary:
        body_parts.append(event.summary)
    raw = " ".join(body_parts)
    if len(raw) <= AlertSmsSender._BODY_MAX_CHARS:
        return raw
    return raw[: AlertSmsSender._BODY_MAX_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# Routing helper
# ---------------------------------------------------------------------------


def _send_by_kind(
    event: AlertEvent,
    channel: AlertChannel,
    get_credential: GetCredential,
    *,
    smtp: AlertSmtpSender,
    webhook: AlertWebhookSender,
    sms: AlertSmsSender,
) -> bool:
    """Route to the right sender by channel.kind; return True on success."""
    try:
        if channel.kind == "email":
            return smtp.send(event, channel, get_credential)
        if channel.kind == "webhook":
            return webhook.send(event, channel, get_credential)
        if channel.kind == "sms":
            return sms.send(event, channel, get_credential)
    except Exception:
        # Credential providers and injected senders may raise before the
        # concrete sender's transport guard. Persist a retry without logging
        # an exception message that could contain a credential or destination.
        _LOG.warning("alert transport raised for channel %s", channel.channel_id)
        return False
    _LOG.warning(  # type: ignore[unreachable]
        "unknown channel kind %r for channel %s", channel.kind, channel.channel_id
    )
    return False


# ---------------------------------------------------------------------------
# Dispatch hook (injected into AlertEvaluator)
# ---------------------------------------------------------------------------


class AlertDeliveryDispatch:
    """Concrete DispatchHook injected into AlertEvaluator.

    Signature matches DispatchHook = Callable[[str, str, str, str, str], None]:
    (event_id, delivery_id, channel_id, condition, label) -> None.

    After the evaluator writes an optimistic "sent" delivery row, this hook
    tries the actual transport. On failure it marks the delivery "failed" with
    a next_attempt_at so AlertRetryWorker will reattempt.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        get_credential: GetCredential,
        retry_settings: AlertRetrySettings | None = None,
        *,
        smtp_sender: AlertSmtpSender | None = None,
        webhook_sender: AlertWebhookSender | None = None,
        sms_sender: AlertSmsSender | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._get_credential = get_credential
        self._settings = retry_settings or AlertRetrySettings()
        self._smtp = smtp_sender or AlertSmtpSender()
        self._webhook = webhook_sender or AlertWebhookSender()
        self._sms = sms_sender or AlertSmsSender()

    def __call__(
        self,
        event_id: str,
        delivery_id: str,
        channel_id: str,
        condition: str,
        label: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            event = get_alert_event(session, event_id)
            channel = get_alert_channel(session, channel_id)
            if event is None or channel is None:
                _LOG.warning(
                    "dispatch: event %s or channel %s not found; dead-lettering delivery %s",
                    event_id,
                    channel_id,
                    delivery_id,
                )
                dead_letter_delivery(session, delivery_id)
                session.commit()
                return
            ok = _send_by_kind(
                event,
                channel,
                self._get_credential,
                smtp=self._smtp,
                webhook=self._webhook,
                sms=self._sms,
            )
            if not ok:
                next_attempt = now + timedelta(seconds=self._settings.base_backoff_seconds)
                fail_delivery(
                    session, delivery_id, error="transport error", next_attempt_at=next_attempt
                )
            else:
                succeed_delivery(session, delivery_id)
            session.commit()


# ---------------------------------------------------------------------------
# Retry worker
# ---------------------------------------------------------------------------


class AlertRetryWorker:
    """Background worker that reattempts failed alert deliveries.

    Call tick() from the daemon's background loop (e.g. every 60s). It polls
    for "failed" delivery rows whose next_attempt_at has elapsed, reattempts
    each, and either marks them "sent" or schedules the next retry with doubled
    backoff. Dead-letters after max_attempts and fires a "service-down" condition
    so the operator sees the persistent channel failure on the dashboard.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        get_credential: GetCredential,
        settings: AlertRetrySettings | None = None,
        *,
        smtp_sender: AlertSmtpSender | None = None,
        webhook_sender: AlertWebhookSender | None = None,
        sms_sender: AlertSmsSender | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._get_credential = get_credential
        self._settings = settings or AlertRetrySettings()
        self._smtp = smtp_sender or AlertSmtpSender()
        self._webhook = webhook_sender or AlertWebhookSender()
        self._sms = sms_sender or AlertSmsSender()

    def tick(self, now: datetime | None = None) -> int:
        """Reattempt all due-retry deliveries; return count of rows processed."""
        if now is None:
            now = datetime.now(UTC)
        processed = 0
        with self._session_factory() as session:
            due = due_retry_deliveries(session, now)
            for delivery in due:
                self._retry_one(session, delivery, now)
                session.commit()
                processed += 1
        return processed

    def _retry_one(self, session: Session, delivery: AlertEventDelivery, now: datetime) -> None:
        event = get_alert_event(session, delivery.event_id)
        channel = get_alert_channel(session, delivery.alert_channel_id)
        if event is None or channel is None:
            _LOG.warning(
                "retry: event %s or channel %s missing; dead-lettering %s",
                delivery.event_id,
                delivery.alert_channel_id,
                delivery.delivery_id,
            )
            dead_letter_delivery(session, delivery.delivery_id)
            return

        ok = _send_by_kind(
            event,
            channel,
            self._get_credential,
            smtp=self._smtp,
            webhook=self._webhook,
            sms=self._sms,
        )
        if ok:
            succeed_delivery(session, delivery.delivery_id)
            return

        new_attempts = delivery.attempts + 1
        if new_attempts >= self._settings.max_attempts:
            dead_letter_delivery(session, delivery.delivery_id)
            _LOG.error(
                "dead-lettering delivery %s (channel %s, event %s) after %d attempts",
                delivery.delivery_id,
                delivery.alert_channel_id,
                delivery.event_id,
                new_attempts,
            )
            record_alert_condition(
                session,
                kind="service-down",
                resource_ref=f"alert_channel:{delivery.alert_channel_id}",
                source_section="S8",
                summary=f"Alert channel {delivery.alert_channel_id!r} unreachable after {new_attempts} attempts",
            )
        else:
            backoff = self._settings.base_backoff_seconds * (2 ** (new_attempts - 1))
            fail_delivery(
                session,
                delivery.delivery_id,
                error="transport error",
                next_attempt_at=now + timedelta(seconds=backoff),
            )
