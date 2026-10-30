# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic v0.8 notification delivery adapters."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal, Protocol

from civiccast.subscribe.crypto import sign_payload
from civiccast.subscribe.models import NotificationDelivery, NotificationPayload, SubscriptionRecord

#: Anything shaped like an address or a URL is scrubbed from persisted or
#: logged failure text. Real adapters put the recipient in the exception --
#: ``smtplib.SMTPRecipientsRefused`` carries the address dict verbatim, and an
#: httpx transport error carries the webhook URL -- so an un-redacted
#: ``str(exc)`` would put subscriber PII straight into the delivery-outcome
#: table, the Publish JSON and the operator log.
_ADDRESS_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_PATTERN = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_MAX_DETAIL_CHARS = 200


def redact_delivery_detail(text: str, *, handle: str | None = None) -> str:
    """Return ``text`` with subscriber addresses, URLs and ``handle`` removed.

    Truncated to a short operator-readable sentence: this string reaches a
    durable table and an API response, so it must stay a *reason*, never a
    recipient.
    """

    scrubbed = text or ""
    if handle:
        scrubbed = scrubbed.replace(handle, "[redacted recipient]")
    scrubbed = _URL_PATTERN.sub("[redacted url]", scrubbed)
    scrubbed = _ADDRESS_PATTERN.sub("[redacted address]", scrubbed)
    scrubbed = " ".join(scrubbed.split())
    if len(scrubbed) > _MAX_DETAIL_CHARS:
        scrubbed = scrubbed[: _MAX_DETAIL_CHARS - 1].rstrip() + "…"
    return scrubbed


def delivery_error_code(exc: BaseException) -> str:
    """Stable, non-secret error code for a failed delivery: the exception type."""

    return type(exc).__name__[:80]


class DeliveryRecorder(Protocol):
    """Durable-outcome hook :func:`dispatch_notifications` calls per recipient.

    Kept as a protocol so the transport fan-out stays free of publication
    semantics: ``civiccast.publish.notifications`` supplies the implementation
    that claims a logical delivery key, skips already-sent recipients and
    persists numbered attempts.
    """

    def should_send(
        self, *, subscription: SubscriptionRecord, target_type: str, target_id: str
    ) -> bool: ...

    def record(
        self,
        *,
        subscription: SubscriptionRecord,
        target_type: str,
        target_id: str,
        outcome: Literal["sent", "failed", "queued"],
        error_code: str | None = None,
        detail: str = "",
        retry_id: str | None = None,
    ) -> None: ...


class MailboxProvider(Protocol):
    def send_confirmation(self, *, email: str, confirmation_url: str) -> str: ...
    def send_notification(self, *, email: str, payload: NotificationPayload) -> str: ...


class WebhookProvider(Protocol):
    def post(self, *, url: str, payload: NotificationPayload, secret: str) -> str: ...
    def send_confirmation(self, *, url: str, confirmation_token: str, secret: str) -> None: ...


def notification_body(payload: NotificationPayload) -> str:
    """The resident-facing notice text, identical for the mock and real mailbox.

    Podcast is omitted entirely when there is no episode: the old body printed
    "Podcast: Not posted" to every resident on every notice, which reads as a
    broken promise about a surface this beta does not offer at all.

    The unsubscribe line is not optional decoration -- a government
    notification list that a resident cannot leave from the message itself is
    a consent failure, so it is part of the shared body rather than something
    each adapter remembers to add.
    """

    lines = [payload.title, f"Watch: {payload.portal_url}"]
    if payload.podcast_url:
        lines.append(f"Podcast: {payload.podcast_url}")
    if payload.unsubscribe_url:
        lines.append("")
        lines.append(f"Stop receiving these notices: {payload.unsubscribe_url}")
    return "\n".join(lines)


class LocalMailbox:
    """In-memory mailbox for CI and local proof."""

    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []

    def send_confirmation(self, *, email: str, confirmation_url: str) -> str:
        message_id = f"mail:{len(self.messages) + 1}"
        self.messages.append(
            {
                "id": message_id,
                "to": email,
                "subject": "Confirm your CivicCast subscription",
                "body": f"Confirm your subscription: {confirmation_url}",
            }
        )
        return message_id

    def send_notification(self, *, email: str, payload: NotificationPayload) -> str:
        message_id = f"mail:{len(self.messages) + 1}"
        self.messages.append(
            {
                "id": message_id,
                "to": email,
                "subject": f"New CivicCast recording: {payload.title}",
                "body": notification_body(payload),
                # Mirrors the header SmtpMailbox sets, so a proof run can assert
                # the unsubscribe contract without a real relay.
                "list_unsubscribe": f"<{payload.unsubscribe_url}>"
                if payload.unsubscribe_url
                else "",
            }
        )
        return message_id


class LocalWebhookClient:
    """In-memory webhook client that records signed payload attempts."""

    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def send_confirmation(self, *, url: str, confirmation_token: str, secret: str) -> None:
        self.requests.append(
            {
                "url": url,
                "confirmation_token": confirmation_token,
                "signing_secret": secret,
            }
        )

    def post(self, *, url: str, payload: NotificationPayload, secret: str) -> str:
        signature = sign_payload(payload.model_dump(mode="json"), secret)
        self.requests.append(
            {
                "url": url,
                "signature": signature,
                "asset_id": payload.asset_id,
            }
        )
        return signature


def delivery_proof(
    *,
    subscription: SubscriptionRecord,
    payload: NotificationPayload,
    message: str,
    signature: str | None = None,
    status: Literal["sent", "failed"] = "sent",
) -> NotificationDelivery:
    return NotificationDelivery(
        delivery_id=f"{subscription.subscription_id}:{payload.asset_id}:{len(message)}",
        subscription_id=subscription.subscription_id,
        channel=subscription.channel,
        status=status,
        target_type=subscription.target_type,
        target_id=subscription.target_id,
        dispatched_at=datetime.now(UTC),
        message=message,
        signature=signature,
    )
