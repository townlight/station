# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real subscriber delivery for the Publish subscriber-notifications surface.

WP-05 (audit findings ENG-001, UX-003, TEST-001, QA-003, DOC-004). The defect
this module closes: ``approve_publish`` built a
:class:`~civiccast.subscribe.models.NotificationPayload`, marked the surface
``succeeded``, and returned. It never called
:func:`civiccast.subscribe.service.dispatch_notifications`. No subscriber was
ever contacted, and a green surface was the only thing an operator saw.

What replaces it:

* the canonical resolver (:mod:`civiccast.publish.targets`) says which targets
  this asset publishes to, and the same resolver filters the public RSS feed;
* the existing SMTP / HMAC-webhook adapters, encrypted subscriber store and
  webhook retry queue do the actual sending -- none of that is reimplemented;
* every attempt lands in the durable ``notification_delivery_outcomes`` table
  under a stable logical key, so re-approval is idempotent and a partial or
  queued fan-out cannot present as fully successful.

Podcast timing: the owner's 2026-09-01 decision turned Podcast into a "coming
soon" card, so there is no podcast job in this beta and never a podcast URL to
wait for. Notices are portal-only and go immediately. The plan's
"wait for Podcast terminal state" rule is deliberately not implemented -- there
is nothing to wait for, and a timer with no job behind it would be a fiction.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from civiccast.platform.providers import (
    PROVIDER_KIND_MAIL,
    PROVIDER_KIND_WEBHOOK,
    ProviderRegistry,
    default_registry,
    describe_provider,
)
from civiccast.publish.models import (
    PublishNotificationDeliveryRow,
    PublishNotificationSummary,
    PublishSurfaceStateValue,
)
from civiccast.publish.store import PublishStore
from civiccast.publish.targets import (
    PublicationTarget,
    publication_id_for_asset,
    resolve_public_base_url,
)
from civiccast.subscribe.delivery import LocalMailbox, LocalWebhookClient
from civiccast.subscribe.models import (
    NotificationDeliveryOutcomeRecord,
    NotificationPayload,
    SubscriptionRecord,
)
from civiccast.subscribe.outcome_store import (
    InMemoryNotificationDeliveryStore,
    NotificationDeliveryStore,
)
from civiccast.subscribe.secrets import SubscriptionSecrets
from civiccast.subscribe.service import dispatch_notifications
from civiccast.subscribe.store import SubscribeStore

_LOG = logging.getLogger(__name__)

#: Cap on per-delivery rows embedded in the publish run's JSON column. The
#: aggregate counts are never capped, and the full history stays in
#: ``notification_delivery_outcomes``.
NOTIFICATION_SUMMARY_MAX_DELIVERIES = 200

__all__ = [
    "NOTIFICATION_SUMMARY_MAX_DELIVERIES",
    "NotificationSurfaceOutcome",
    "build_notification_payload",
    "deliver_publication_notifications",
    "refresh_notification_surface",
    "summarize_outcomes",
    "watch_url_for_asset",
]


def watch_url_for_asset(asset_id: str, *, public_base_url: str | None = None) -> str | None:
    """The recording's real public watch URL, or ``None`` when unconfigured.

    ``#/watch/{asset_id}`` is the portal-public router's actual route
    (``civiccast/apps/portal-public/src/router.ts``). Returns ``None`` rather
    than a ``portal.example`` placeholder when the station has no public base
    URL configured -- an unset base URL is an operator gap to surface, not a
    hostname to invent.
    """

    base = public_base_url if public_base_url is not None else resolve_public_base_url()
    if not base:
        return None
    # ``quote`` matches the portal router's own ``encodeURIComponent`` so the
    # link this notice carries resolves to the same route the portal builds.
    return f"{base.rstrip('/')}/#/watch/{quote(asset_id, safe='')}"


def build_notification_payload(
    *,
    asset_id: str,
    title: str,
    published_at: datetime,
    manifest_url: str | None = None,
    public_base_url: str | None = None,
    summary: str | None = None,
) -> NotificationPayload | None:
    """Build the notice, or ``None`` when there is no real URL to send.

    Prefers the station's public watch page over the raw HLS manifest: the
    manifest is a player input, not something to put in a resident's inbox.
    ``podcast_url`` is always ``None`` -- Podcast is a "coming soon" card in
    this beta, and a link to a feed that does not exist is worse than no link.
    """

    portal_url = watch_url_for_asset(asset_id, public_base_url=public_base_url) or manifest_url
    if not portal_url:
        return None
    return NotificationPayload(
        asset_id=asset_id,
        title=title,
        portal_url=portal_url,
        podcast_url=None,
        summary=summary or f"New CivicCast recording published: {title}.",
        published_at=published_at,
    )


@dataclass(frozen=True)
class NotificationSurfaceOutcome:
    """Observed result of one subscriber-notification dispatch."""

    state: PublishSurfaceStateValue
    health: str
    message: str
    next_step: str
    #: True when the mail/webhook adapters that accepted this run's notices are
    #: the shipped mocks. The dashboard badges it exactly as it badges a
    #: simulated Internet Archive or YouTube surface.
    simulated: bool
    summary: PublishNotificationSummary | None


class _OutcomeRecorder:
    """Binds :func:`dispatch_notifications` to the durable outcome table.

    ``should_send`` is the duplicate-send guard: it claims the logical delivery
    key first and sends only when the store GRANTS it -- i.e. this caller
    inserted the row, or took over one whose in-flight lease had lapsed, or is
    an explicit retry taking over a failed/queued row. A recipient another
    worker currently holds, or one already observed ``sent``, is refused.
    ``record`` persists the attempt immediately, so an exception on a later
    recipient cannot erase an earlier receipt.

    Both methods absorb their own storage failures rather than letting one bad
    row abort the fan-out (plan item 7). The two failures are handled
    differently on purpose: an unwritable *claim* means there is no
    duplicate-send guard for that recipient, so the recipient is NOT contacted
    (a missing notice is recoverable; a duplicate one is not), while an
    unwritable *receipt* only costs the receipt -- the notice already went out,
    and the missing row makes the surface read non-green, which is the honest
    outcome.
    """

    def __init__(
        self,
        *,
        store: NotificationDeliveryStore,
        publication_id: str,
        asset_id: str,
        retry_only: bool,
    ) -> None:
        self._store = store
        self._publication_id = publication_id
        self._asset_id = asset_id
        self._retry_only = retry_only
        self._keys: dict[str, str] = {}
        #: Recipients whose receipt could not be reserved at all. A run with any
        #: of these cannot honestly report "no subscribers" or "all delivered"
        #: -- the operator is told the database is the problem (see
        #: ``deliver_publication_notifications``).
        self.claim_failures = 0

    def should_send(
        self, *, subscription: SubscriptionRecord, target_type: str, target_id: str
    ) -> bool:
        try:
            claimed = self._store.claim(
                publication_id=self._publication_id,
                asset_id=self._asset_id,
                subscription_id=subscription.subscription_id,
                target_type=target_type,
                target_id=target_id,
                transport=subscription.channel,
                # An explicit retry means "reach every confirmed subscriber not
                # yet sent" -- including one who confirmed AFTER the original
                # approval (they get a fresh row) and one whose delivery is
                # terminal-failed. Queued webhooks stay with their retry worker. A
                # normal re-approval takes over neither, which is what makes it
                # idempotent instead of a second fan-out.
                allow_reattempt=self._retry_only,
            )
        except Exception:
            self.claim_failures += 1
            _LOG.exception(
                "Could not reserve a delivery receipt for subscription %s; skipping it "
                "rather than sending a notice this run cannot guard against duplicating.",
                subscription.subscription_id,
            )
            return False
        self._keys[subscription.subscription_id] = claimed.record.delivery_key
        if not claimed.granted:
            _LOG.debug(
                "Skipping subscription %s: %s.",
                subscription.subscription_id,
                "already delivered for this publication"
                if claimed.already_sent
                else "another worker holds this delivery",
            )
            return False
        if not claimed.created:
            _LOG.info(
                "Took over an existing delivery for subscription %s (outcome %s).",
                subscription.subscription_id,
                claimed.record.outcome,
            )
        return True

    def record(
        self,
        *,
        subscription: SubscriptionRecord,
        target_type: str,
        target_id: str,
        outcome: str,
        error_code: str | None = None,
        detail: str = "",
        retry_id: str | None = None,
    ) -> None:
        key = self._keys.get(subscription.subscription_id)
        if key is None:  # pragma: no cover - should_send always runs first
            return
        try:
            self._store.record_attempt(
                delivery_key=key,
                outcome=outcome,  # type: ignore[arg-type]
                error_code=error_code,
                detail=detail,
                retry_id=retry_id,
            )
        except Exception:
            _LOG.exception(
                "Delivery for subscription %s was attempted but its receipt could not be "
                "written; the surface will report a non-green state for it.",
                subscription.subscription_id,
            )


def summarize_outcomes(
    records: Sequence[NotificationDeliveryOutcomeRecord],
    *,
    publication_id: str,
    targets: Sequence[PublicationTarget] = (),
) -> PublishNotificationSummary:
    """Fold durable outcome rows into the safe summary the dashboard renders."""

    rows = [
        PublishNotificationDeliveryRow(
            subscription_id=record.subscription_id,
            channel=record.transport,
            target_type=record.target_type,
            target_id=record.target_id,
            outcome=record.outcome,
            attempts=record.attempts,
            error_code=record.error_code,
            detail=record.detail,
            retry_id=record.retry_id,
            last_attempted_at=record.last_attempted_at,
        )
        for record in sorted(records, key=lambda record: record.delivery_key)
    ]
    return PublishNotificationSummary(
        publication_id=publication_id,
        intended=len(rows),
        sent=sum(1 for row in rows if row.outcome == "sent"),
        failed=sum(1 for row in rows if row.outcome == "failed"),
        queued=sum(1 for row in rows if row.outcome == "queued"),
        pending=sum(1 for row in rows if row.outcome == "pending"),
        targets=[f"{target.target_type}:{target.target_id}" for target in targets],
        # Counts are always complete; only the row list is bounded, because
        # this summary is embedded in the publish run's JSON column. Rows that
        # need attention (anything not "sent") are listed first, so a truncated
        # list still shows the operator what is wrong.
        deliveries_truncated=len(rows) > NOTIFICATION_SUMMARY_MAX_DELIVERIES,
        deliveries=sorted(rows, key=lambda row: (row.outcome == "sent", row.subscription_id))[
            :NOTIFICATION_SUMMARY_MAX_DELIVERIES
        ],
    )


def _state_for(summary: PublishNotificationSummary) -> PublishSurfaceStateValue:
    """Closed-vocabulary state, applied in the plan's precedence order.

    ``succeeded`` > ``partial`` > ``pending`` > ``failed`` > ``not_configured``.
    The order is what stops a delivery that is still retrying from being
    reported as terminally failed, and stops a fan-out with one dead recipient
    from being reported as fully successful.
    """

    if summary.intended == 0:
        return "not_configured"
    if summary.sent == summary.intended:
        return "succeeded"
    if summary.sent > 0:
        return "partial"
    if summary.queued > 0 or summary.pending > 0:
        return "pending"
    return "failed"


def refresh_notification_surface(
    *,
    asset_id: str,
    delivery_store: NotificationDeliveryStore,
    publish_store: PublishStore,
) -> None:
    """Refresh the saved dashboard snapshot after a background webhook retry."""
    run = publish_store.get_run(asset_id)
    if run is None:
        return
    previous = next((s for s in run.surfaces if s.id == "subscriber-notifications"), None)
    if previous is None or previous.notification_summary is None:
        return
    summary = summarize_outcomes(
        delivery_store.list_for_publication(previous.notification_summary.publication_id),
        publication_id=previous.notification_summary.publication_id,
    ).model_copy(update={"targets": previous.notification_summary.targets})
    state = _state_for(summary)
    health, message_template, next_step = _STATE_COPY[state]
    if previous.simulated:
        health = "warning"
        message_template = (
            "SIMULATED - no subscriber notice actually left this station. " + message_template
        )
    refreshed = previous.model_copy(
        update={
            "state": state,
            "health": health,
            "message": message_template.format(
                sent=summary.sent,
                intended=summary.intended,
                queued=summary.queued,
                failed=summary.failed,
                pending=summary.pending,
            ),
            "next_step": next_step,
            "notification_summary": summary,
        }
    )
    publish_store.upsert_run(
        run.model_copy(
            update={"surfaces": [refreshed if s.id == refreshed.id else s for s in run.surfaces]}
        )
    )


_STATE_COPY: dict[str, tuple[str, str, str]] = {
    # state -> (health, message template, next step)
    "succeeded": (
        "ok",
        "Every confirmed subscriber notice was delivered ({sent} of {intended}).",
        "No action needed. Delivery receipts are kept with this publish run.",
    ),
    "partial": (
        "warning",
        "Some subscriber notices were not delivered: {sent} of {intended} delivered, "
        "{queued} still retrying, {failed} failed.",
        "Open the delivery list. The worker will retry queued webhooks; retry the "
        "surface after fixing a terminal failure.",
    ),
    "pending": (
        "warning",
        "No subscriber notice has been delivered yet: {queued} are queued for retry "
        "and {pending} have not been attempted.",
        "The retry worker is still trying. Check again shortly.",
    ),
    "failed": (
        "warning",
        "No subscriber notice could be delivered ({failed} of {intended} failed).",
        "Fix the mail or webhook problem shown in the delivery list, then retry this surface.",
    ),
    "not_configured": (
        "unknown",
        "No confirmed subscribers are targeted, so nothing was sent.",
        "No action needed. Notices start once residents confirm a subscription "
        "for this channel or meeting body.",
    ),
}


def deliver_publication_notifications(
    *,
    asset_id: str,
    title: str,
    published_at: datetime,
    targets: Sequence[PublicationTarget],
    manifest_url: str | None = None,
    public_base_url: str | None = None,
    subscribe_store: SubscribeStore | None,
    delivery_store: NotificationDeliveryStore | None = None,
    registry: ProviderRegistry | None = None,
    secrets: SubscriptionSecrets | None = None,
    mailbox: LocalMailbox | None = None,
    webhook_client: LocalWebhookClient | None = None,
    retry_only: bool = False,
) -> NotificationSurfaceOutcome:
    """Deliver this publication's notices and report what actually happened.

    Never raises for a delivery problem: every per-recipient failure is caught
    and persisted by :func:`dispatch_notifications`, and the aggregate state is
    read back from the durable table afterwards, so the surface state is an
    observation of stored receipts rather than of this call's control flow.

    ``registry`` is the app's single provider registry, so the mail/webhook
    adapters used here are the SAME ones publish preflight reported readiness
    for. Passing an explicit ``mailbox``/``webhook_client`` overrides it.
    """

    if subscribe_store is None:
        return NotificationSurfaceOutcome(
            state="not_configured",
            health="unknown",
            message="The subscriber store is unavailable, so no notices were sent.",
            next_step=(
                "Open Setup and choose Prepare storage so subscriptions and delivery "
                "receipts can be kept."
            ),
            simulated=False,
            summary=None,
        )

    publication_id = publication_id_for_asset(asset_id)
    payload = build_notification_payload(
        asset_id=asset_id,
        title=title,
        published_at=published_at,
        manifest_url=manifest_url,
        public_base_url=public_base_url,
    )
    if payload is None:
        return NotificationSurfaceOutcome(
            state="not_configured",
            health="unknown",
            message=(
                "Subscriber notices were not sent: this station has no public web "
                "address, so the notice would have no link to open."
            ),
            next_step=(
                "Set the station's public web address in Setup, then retry the "
                "subscriber notifications surface."
            ),
            simulated=False,
            summary=None,
        )

    # A durable store is strongly preferred; an ephemeral instance still gets a
    # real (process-local) guard rather than silently losing every receipt.
    store = delivery_store if delivery_store is not None else InMemoryNotificationDeliveryStore()
    recorder = _OutcomeRecorder(
        store=store,
        publication_id=publication_id,
        asset_id=asset_id,
        retry_only=retry_only,
    )
    resolved_registry = registry if registry is not None else default_registry()
    response = dispatch_notifications(
        payload,
        store=subscribe_store,
        targets=[(target.target_type, target.target_id) for target in targets],
        secrets=secrets,
        mailbox=mailbox if mailbox is not None else resolved_registry.resolve(PROVIDER_KIND_MAIL),
        webhook_client=(
            webhook_client
            if webhook_client is not None
            else resolved_registry.resolve(PROVIDER_KIND_WEBHOOK)
        ),
        unsubscribe_base_url=public_base_url
        if public_base_url is not None
        else resolve_public_base_url(),
        recorder=recorder,
    )

    summary = summarize_outcomes(
        store.list_for_publication(publication_id),
        publication_id=publication_id,
        targets=targets,
    )
    if recorder.claim_failures:
        # M3: an unwritable receipt store must never read as "no subscribers"
        # or as a clean run. The operator is told the database is the problem,
        # in those words, because that is what they have to fix.
        _LOG.error(
            "Publish %s subscriber notifications: %d delivery receipts could not be "
            "written, so those notices were not sent.",
            asset_id,
            recorder.claim_failures,
        )
        return NotificationSurfaceOutcome(
            state="failed",
            health="warning",
            message=(
                "Delivery receipts could not be written, so no notices were sent. "
                "Fix the database and retry."
            ),
            next_step=(
                "Open Setup and check durable storage, then retry the subscriber "
                "notifications surface."
            ),
            simulated=False,
            summary=summary if summary.intended else None,
        )

    state = _state_for(summary)
    health, message_template, next_step = _STATE_COPY[state]
    simulated = _simulated_channels(summary, registry=resolved_registry)
    message = message_template.format(
        sent=summary.sent,
        intended=summary.intended,
        queued=summary.queued,
        failed=summary.failed,
        pending=summary.pending,
    )
    if simulated and state != "not_configured":
        # M4, matching the Internet Archive / YouTube branches: a mock adapter
        # accepted the notice, nobody received it. Never green.
        health = "warning"
        message = (
            f"SIMULATED - no subscriber notice actually left this station. {message} "
            "An admin must enable the real mail/webhook provider before this counts "
            "as delivery."
        )
        next_step = (
            "Ask an admin to set CIVICCAST_PROVIDER_MAIL / CIVICCAST_PROVIDER_WEBHOOK "
            "to a real provider, then retry this surface."
        )
    _LOG.info(
        "Publish %s subscriber notifications: %s (%d intended, %d sent, %d queued, "
        "%d failed, %d already delivered).",
        asset_id,
        state,
        summary.intended,
        summary.sent,
        summary.queued,
        summary.failed,
        response.skipped,
    )
    return NotificationSurfaceOutcome(
        state=state,
        health=health,
        message=message,
        next_step=next_step,
        simulated=simulated,
        summary=summary if summary.intended else None,
    )


def _simulated_channels(summary: PublishNotificationSummary, *, registry: ProviderRegistry) -> bool:
    """True when any channel this run used resolves to a mock adapter.

    Read from the same registry the send itself resolved, and only for channels
    that actually had a recipient -- a station with no webhook subscribers is
    not "simulated" because its unused webhook provider is the shipped mock.
    """

    kinds = {
        "email": PROVIDER_KIND_MAIL,
        "webhook": PROVIDER_KIND_WEBHOOK,
    }
    used = {row.channel for row in summary.deliveries}
    return any(describe_provider(kinds[channel], registry).simulated for channel in sorted(used))
