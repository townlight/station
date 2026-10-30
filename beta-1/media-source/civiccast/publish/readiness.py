# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real provider-registry-backed readiness for Publish preflight and approval.

WP-03 (audit findings QA-001 / ENG-001 readiness portion): preflight and
approval used to read readiness from two different sources -- preflight from
a deterministic mock credential store (``civiccast.publish.credentials``,
now removed) that always reported "healthy" unless a test explicitly told it
otherwise, while approval resolved the real ``civiccast.platform.providers``
registry. A station that set ``CIVICCAST_PROVIDER_YOUTUBE=real`` with no
credentials therefore saw preflight say ``ready=true`` and then got an
uncaught ``ProviderConfigurationError`` (a 500) on approval.

This module is the single readiness source both preflight
(:func:`civiccast.publish.service.build_publish_preflight`) and approval
(:func:`civiccast.publish.service.approve_publish`) call, so they read the
same registry and cannot disagree about configuration readiness (plan item
8). It never performs a network call or write -- ``describe_provider`` is
documented side-effect-free, and this module only adds read-only lookups on
top of it.

Subscriber notification readiness checks the same resolved publication
targets and provider registry used by delivery. A selected surface with
missing configuration reports that condition rather than claiming a send.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from civiccast.platform.providers import (
    PROVIDER_KIND_INTERNET_ARCHIVE,
    PROVIDER_KIND_LOCAL_NAS,
    PROVIDER_KIND_MAIL,
    PROVIDER_KIND_WEBHOOK,
    PROVIDER_KIND_YOUTUBE,
    ProviderRegistry,
    describe_provider,
)
from civiccast.subscribe.models import SubscriptionRecord
from civiccast.subscribe.store import SubscribeStore

__all__ = [
    "SurfaceReadiness",
    "describe_surface_readiness",
]

# Surfaces whose readiness is answered by exactly one provider kind.
_SURFACE_PROVIDER_KIND: dict[str, str] = {
    "internet-archive": PROVIDER_KIND_INTERNET_ARCHIVE,
    "local-nas-rsync": PROVIDER_KIND_LOCAL_NAS,
    "local-nas-zfs": PROVIDER_KIND_LOCAL_NAS,
    "youtube-live": PROVIDER_KIND_YOUTUBE,
    "youtube-vod": PROVIDER_KIND_YOUTUBE,
}

_SUBSCRIBER_CHANNEL_KIND: dict[str, str] = {
    "email": PROVIDER_KIND_MAIL,
    "webhook": PROVIDER_KIND_WEBHOOK,
}

# Podcast produces a local media artifact; it has no external provider kind.
FUTURE_SURFACE_IDS: frozenset[str] = frozenset()

# WP-05: readiness used to evaluate one hardcoded ``channel/government``
# target while delivery fanned out to the asset's REAL resolved targets, so
# preflight could report "no confirmed subscribers" for a meeting-body
# recording that then mailed a dozen residents (or vice versa). Callers now
# pass the same targets ``civiccast.publish.targets
# .resolve_publication_targets`` gives the delivery path; there is no default,
# because a default is exactly how the two drifted apart.


@dataclass(frozen=True)
class SurfaceReadiness:
    """Non-secret readiness verdict for one publish surface.

    ``reference`` never carries a credential value -- only the env var name
    and the selected provider name (e.g. ``CIVICCAST_PROVIDER_YOUTUBE=real``),
    matching the shipped provider settings classes, which name missing
    variables and never their values (see
    ``civiccast.platform.providers.describe_provider``).
    """

    healthy: bool
    reference: str
    message: str
    next_step: str


def _env_reference(kind: str, selected_name: str) -> str:
    return f"CIVICCAST_PROVIDER_{kind.upper()}={selected_name}"


def _provider_readiness(kind: str, *, label: str, registry: ProviderRegistry) -> SurfaceReadiness:
    config = describe_provider(kind, registry)
    reference = _env_reference(kind, config.selected_name)
    env_key = reference.split("=", 1)[0]
    if not config.usable:
        # A selected-real (or otherwise misconfigured) provider: this is the
        # "missing/invalid selected-real configuration" case from the plan --
        # unhealthy, so preflight reports ready=false and approval refuses
        # with 409 before any side effect (plan items 4-5).
        return SurfaceReadiness(
            healthy=False,
            reference=reference,
            message=f"{label} cannot publish: {config.error}",
            next_step=f"Fix the {env_key}=real configuration, then rerun preflight.",
        )
    if config.simulated:
        # The shipped mock default: usable (an operator CAN approve this
        # surface today), but explicitly marked simulated so it is never
        # mistaken for real-provider proof (plan item 6).
        return SurfaceReadiness(
            healthy=True,
            reference=reference,
            message=(
                f"{label} preflight is simulated (mock provider); this is a warning, "
                "not proof the real provider is reachable."
            ),
            next_step=(
                f"Ask an admin to set {env_key}=real with valid credentials before this "
                "counts as real-provider proof."
            ),
        )
    return SurfaceReadiness(
        healthy=True,
        reference=reference,
        message=f"{label} is configured with the real {config.selected_name} provider.",
        next_step="No action required for this provider check.",
    )


def _confirmed_across_targets(
    store: SubscribeStore, targets: Sequence[tuple[str, str]]
) -> list[SubscriptionRecord]:
    """Confirmed subscriptions across every target, deduplicated by subscription.

    Mirrors the delivery path's own dedupe rule
    (``civiccast.subscribe.service._intended_deliveries``) so readiness counts
    the same recipient set the send would.
    """

    seen: set[str] = set()
    found: list[SubscriptionRecord] = []
    for target_type, target_id in targets:
        for record in store.list_confirmed_for_target(target_type=target_type, target_id=target_id):
            if record.subscription_id in seen:
                continue
            seen.add(record.subscription_id)
            found.append(record)
    return found


def _subscriber_readiness(
    *,
    label: str,
    registry: ProviderRegistry,
    store: SubscribeStore | None,
    targets: Sequence[tuple[str, str]],
) -> SurfaceReadiness:
    if store is None:
        # No subscribe store was wired for this call (e.g. an ephemeral app
        # instance, or a caller -- such as the nightly soak runner -- that
        # never touches durable subscriber storage). There is nothing to
        # confirm a recipient against, so there is nothing selected-real to
        # check; this must never read as a failure.
        return SurfaceReadiness(
            healthy=True,
            reference="subscribe-store:unavailable",
            message=f"{label}: the subscriber store is unavailable; there are no recipients to check.",
            next_step="No action required.",
        )
    confirmed = _confirmed_across_targets(store, targets)
    channels_used = sorted({subscription.channel for subscription in confirmed})
    if not channels_used:
        return SurfaceReadiness(
            healthy=True,
            reference="subscribe-store:no-confirmed-recipients",
            message=f"{label}: no confirmed subscribers are targeted; there is nothing to send.",
            next_step="No action required.",
        )
    references: list[str] = []
    problems: list[str] = []
    simulated_any = False
    for channel in channels_used:
        kind = _SUBSCRIBER_CHANNEL_KIND[channel]
        config = describe_provider(kind, registry)
        references.append(_env_reference(kind, config.selected_name))
        if not config.usable:
            problems.append(f"{channel}: {config.error}")
        elif config.simulated:
            simulated_any = True
    reference = "; ".join(references)
    if problems:
        return SurfaceReadiness(
            healthy=False,
            reference=reference,
            message=(
                f"{label} cannot deliver to the confirmed recipients that would be targeted: "
                + "; ".join(problems)
                + "."
            ),
            next_step="Fix the listed CIVICCAST_PROVIDER_* configuration, then rerun preflight.",
        )
    if simulated_any:
        return SurfaceReadiness(
            healthy=True,
            reference=reference,
            message=(
                f"{label} preflight is simulated for at least one confirmed recipient channel; "
                "this is a warning, not proof of real delivery."
            ),
            next_step=(
                "Ask an admin to enable the real mail/webhook provider before this counts as "
                "real delivery proof."
            ),
        )
    return SurfaceReadiness(
        healthy=True,
        reference=reference,
        message=f"{label} is configured with real delivery for every confirmed recipient channel.",
        next_step="No action required for this provider check.",
    )


def describe_surface_readiness(
    surface_id: str,
    *,
    label: str,
    registry: ProviderRegistry,
    subscribe_store: SubscribeStore | None = None,
    subscribe_targets: Sequence[tuple[str, str]] = (),
) -> SurfaceReadiness | None:
    """Return ``surface_id``'s real readiness, or ``None`` if it has none to check.

    ``None`` covers surfaces that never route through the provider registry
    at all: Portal (gated on ``manifest_url``, not a provider), Podcast, and the
    Cable file package surface (a local filesystem check owned by
    ``civiccast.cable.package``, out of WP-03's scope). Callers keep those
    surfaces' existing, unrelated readiness logic in
    ``build_publish_preflight`` / ``approve_publish``.
    """

    if surface_id in FUTURE_SURFACE_IDS:
        return None
    if surface_id == "subscriber-notifications":
        return _subscriber_readiness(
            label=label,
            registry=registry,
            store=subscribe_store,
            targets=subscribe_targets,
        )
    kind = _SURFACE_PROVIDER_KIND.get(surface_id)
    if kind is None:
        return None
    return _provider_readiness(kind, label=label, registry=registry)
