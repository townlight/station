# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Publish workflow orchestration and dashboard state derivation."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from civiccast.cable.package import (
    CABLE_PACKAGE_SURFACE_ID,
    CablePackageError,
    CablePackageNotConfiguredError,
    build_cable_file_package_for_asset,
)
from civiccast.platform.broker import BrokerEvent
from civiccast.platform.providers import (
    PROVIDER_KIND_INTERNET_ARCHIVE,
    PROVIDER_KIND_LOCAL_NAS,
    PROVIDER_KIND_YOUTUBE,
    ProviderRegistry,
    default_registry,
)
from civiccast.podcast.models import PodcastEpisodeCreate
from civiccast.podcast.service import PodcastMediaError, create_podcast_episode
from civiccast.podcast.store import PodcastStore
from civiccast.publish.models import (
    PublishApprovalRequest,
    PublishAssetStatus,
    PublishAuditEvent,
    PublishDashboardResponse,
    PublishDashboardStateValue,
    PublishDashboardSummary,
    PublishPreflightCheck,
    PublishPreflightResponse,
    PublishRetryRequest,
    PublishRunRecord,
    PublishSurfaceKindValue,
    PublishSurfaceStatus,
)
from civiccast.publish.notifications import deliver_publication_notifications
from civiccast.publish.readiness import (
    FUTURE_SURFACE_IDS,
    SurfaceReadiness,
    describe_surface_readiness,
)
from civiccast.publish.store import PublishStore
from civiccast.publish.targets import (
    DEFAULT_CHANNEL_ID_FALLBACK,
    ChannelAssociationLookup,
    resolve_publication_targets,
)
from civiccast.schedule.models import StaffAssetRow
from civiccast.subscribe.outcome_store import NotificationDeliveryStore
from civiccast.subscribe.store import SubscribeStore

PUBLIC_RECORD_POLICIES = {"meeting", "permanent"}


class PublishConfigurationError(RuntimeError):
    """One or more operator-selected surfaces have invalid provider config.

    Raised by :func:`approve_publish` BEFORE any surface is executed (WP-03
    plan item 5: a controlled 409, not a mid-approval crash or partial
    publish). ``surfaces`` maps surface id to the readiness verdict that
    blocked it, so the router can build an actionable detail message without
    re-deriving it.
    """

    def __init__(self, surfaces: dict[str, SurfaceReadiness]) -> None:
        self.surfaces = surfaces
        detail = "; ".join(f"{sid}: {r.message}" for sid, r in sorted(surfaces.items()))
        super().__init__(f"Publish preflight blocked: {detail}")


def _is_public_record(asset: StaffAssetRow) -> bool:
    return asset.retention_policy in PUBLIC_RECORD_POLICIES


def build_publish_approved_event(status: PublishAssetStatus) -> BrokerEvent:
    """Build the documented event emitted after a successful publish approval."""

    return BrokerEvent(
        subject="publish.asset.approved",
        payload={
            "asset_id": status.asset_id,
            "status": status.dashboard_state,
            "surfaces": [
                {
                    "id": surface.id,
                    "state": surface.state,
                    "health": surface.health,
                    "required": surface.required,
                }
                for surface in status.surfaces
            ],
        },
    )


def _event(
    *,
    asset_id: str,
    surface: PublishSurfaceStatus,
    action: Literal["approved", "started", "succeeded", "failed", "retried", "overridden"],
    operator_id: str,
    message: str,
    at: datetime,
) -> PublishAuditEvent:
    return PublishAuditEvent(
        event_id=f"{asset_id}:{surface.id}:{action}:{int(at.timestamp())}",
        asset_id=asset_id,
        surface_id=surface.id,
        action=action,
        operator_id=operator_id,
        occurred_at=at,
        message=message,
        url=surface.url,
        path=surface.path,
        verification_hash=surface.verification_hash,
    )


def _blocked_portal_surface(asset: StaffAssetRow) -> PublishSurfaceStatus:
    if asset.manifest_url:
        return PublishSurfaceStatus(
            id="portal",
            label="Portal",
            kind="canonical",
            state="pending",
            required=True,
            approval="pending",
            url=asset.manifest_url,
            health="warning",
            message="Portal publish is waiting for operator approval.",
            next_step="Approve publish to make the canonical portal URL public.",
        )
    return PublishSurfaceStatus(
        id="portal",
        label="Portal",
        kind="canonical",
        state="blocked",
        required=True,
        approval="pending",
        health="error",
        message="The portal cannot publish this asset until an HLS manifest exists.",
        next_step="Run the packager or fix ingest so manifest_url exists, then retry approval.",
    )


def _pending_surface(
    *,
    surface_id: str,
    label: str,
    kind: PublishSurfaceKindValue,
    required: bool,
    message: str,
    next_step: str,
) -> PublishSurfaceStatus:
    return PublishSurfaceStatus(
        id=surface_id,
        label=label,
        kind=kind,
        state="pending",
        required=required,
        approval="pending",
        health="warning" if required else "unknown",
        message=message,
        next_step=next_step,
    )


def default_approved_surface_ids(asset: StaffAssetRow) -> set[str]:
    """Surfaces an approval publishes when the operator names none: canonical only."""

    return {surface.id for surface in build_initial_surfaces(asset) if surface.kind == "canonical"}


def build_initial_surfaces(asset: StaffAssetRow) -> list[PublishSurfaceStatus]:
    """Build per-surface approval rows before a publish run exists."""
    public_record = _is_public_record(asset)
    return [
        _blocked_portal_surface(asset),
        _pending_surface(
            surface_id="internet-archive",
            label="Internet Archive",
            kind="archive",
            required=public_record,
            message=(
                "Internet Archive proof is required before archive verification."
                if public_record
                else "Internet Archive is optional for this non-public-record asset."
            ),
            next_step="Approve IA publish or enter an audit-logged override.",
        ),
        _pending_surface(
            surface_id="local-nas-rsync",
            label="Local NAS rsync",
            kind="archive",
            required=public_record,
            message=(
                "Rsync/hash local NAS proof is required before archive verification."
                if public_record
                else "Local NAS rsync proof is optional for this non-public-record asset."
            ),
            next_step="Approve local NAS rsync archive or enter an audit-logged override.",
        ),
        _pending_surface(
            surface_id="local-nas-zfs",
            label="Local NAS ZFS",
            kind="archive",
            required=public_record,
            message=(
                "ZFS send/hash local NAS proof is required before archive verification."
                if public_record
                else "Local NAS ZFS proof is optional for this non-public-record asset."
            ),
            next_step="Approve local NAS ZFS archive or enter an audit-logged override.",
        ),
        _pending_surface(
            surface_id="youtube-live",
            label="YouTube Live",
            kind="reach",
            required=False,
            message="YouTube Live is a reach surface; failures must not block the portal.",
            next_step="Approve YouTube Live fanout or leave it pending.",
        ),
        _pending_surface(
            surface_id="youtube-vod",
            label="YouTube VOD",
            kind="reach",
            required=False,
            message="YouTube VOD is a reach surface; failures degrade independently.",
            next_step="Approve YouTube VOD upload or leave it pending.",
        ),
        _pending_surface(
            surface_id="podcast",
            label="Podcast episode",
            kind="audience",
            required=False,
            message="Podcast RSS is an audience surface generated after operator approval.",
            next_step="Approve podcast generation or leave it pending for later audio review.",
        ),
        _pending_surface(
            surface_id="subscriber-notifications",
            label="Subscriber notifications",
            kind="audience",
            required=False,
            message="Confirmed subscribers receive notices after publish; failures do not block archive.",
            next_step="Approve notification dispatch after the public portal link is ready.",
        ),
        _pending_surface(
            surface_id=CABLE_PACKAGE_SURFACE_ID,
            label="Cable file package",
            kind="record",
            required=False,
            message=(
                "Cable file package creates a local ZIP with media, captions, metadata, "
                "and hashes for PEG/headend handoff."
            ),
            next_step=(
                "Configure a cable package output folder and caption sidecar folder, then "
                "approve this surface for headend handoff."
            ),
        ),
    ]


def _surface_readiness(
    surface: PublishSurfaceStatus,
    *,
    registry: ProviderRegistry,
    subscribe_store: SubscribeStore | None,
    subscribe_targets: Sequence[tuple[str, str]],
) -> SurfaceReadiness | None:
    """Real readiness for one surface, or ``None`` for a non-provider surface.

    The single readiness source shared by :func:`build_publish_preflight` and
    :func:`approve_publish` (WP-03 plan item 8: preflight and approval read
    the same current registry configuration so they cannot disagree).

    WP-05 extends that agreement to the RECIPIENT set: ``subscribe_targets``
    is the asset's resolved publication targets, so readiness counts exactly
    the subscribers delivery would contact -- including meeting-body ones the
    old hardcoded ``channel/government`` target could never see.
    """

    return describe_surface_readiness(
        surface.id,
        label=surface.label,
        registry=registry,
        subscribe_store=subscribe_store,
        subscribe_targets=subscribe_targets,
    )


def _target_pairs(
    asset: StaffAssetRow, lookup: ChannelAssociationLookup | None
) -> tuple[tuple[str, str], ...]:
    """The asset's resolved publication targets as ``(type, id)`` pairs."""

    return tuple(
        (target.target_type, target.target_id)
        for target in resolve_publication_targets(asset, lookup=lookup)
    )


def build_publish_preflight(
    asset: StaffAssetRow,
    *,
    registry: ProviderRegistry | None = None,
    subscribe_store: SubscribeStore | None = None,
    target_lookup: ChannelAssociationLookup | None = None,
) -> PublishPreflightResponse:
    """Return approval readiness for every v0.7 publish surface.

    Side-effect-free (WP-03 plan item 4): every provider factory this calls
    through ``describe_provider`` only reads/validates environment
    configuration -- it never probes a remote service. Missing or invalid
    selected-real configuration returns ``ready=false`` for that surface with
    a safe (non-secret) credential reference and an actionable next step; it
    never raises.
    """
    resolved_registry = registry if registry is not None else default_registry()
    subscribe_targets = _target_pairs(asset, target_lookup)
    checks: list[PublishPreflightCheck] = []
    for surface in build_initial_surfaces(asset):
        if surface.id == "portal":
            health: Literal["ok", "error"] = "ok" if asset.manifest_url else "error"
            checks.append(
                PublishPreflightCheck(
                    id=surface.id,
                    label=surface.label,
                    kind=surface.kind,
                    required=surface.required,
                    health=health,
                    message=(
                        "Portal manifest is packaged and ready."
                        if asset.manifest_url
                        else "Portal manifest is missing for this asset."
                    ),
                    next_step=(
                        "Approve portal publication when review is complete."
                        if asset.manifest_url
                        else "Run the packager or fix ingest so manifest_url exists."
                    ),
                )
            )
            continue
        readiness = _surface_readiness(
            surface,
            registry=resolved_registry,
            subscribe_store=subscribe_store,
            subscribe_targets=subscribe_targets,
        )
        if readiness is None:
            if surface.id == "podcast":
                checks.append(
                    PublishPreflightCheck(
                        id=surface.id,
                        label=surface.label,
                        kind=surface.kind,
                        required=False,
                        health="unknown",
                        message="Podcast audio is created from the local recording when selected.",
                        next_step="Make sure this asset has a local recording and a public station URL.",
                    )
                )
                continue
            # No provider dependency for this surface (e.g. the Cable file
            # package, which has its own local configuredness check run only
            # at approval time) -- nothing to report as not-ready here.
            checks.append(
                PublishPreflightCheck(
                    id=surface.id,
                    label=surface.label,
                    kind=surface.kind,
                    required=surface.required,
                    health="unknown",
                    message=f"{surface.label} has no external provider dependency to check here.",
                    next_step="No preflight action required.",
                )
            )
            continue
        checks.append(
            PublishPreflightCheck(
                id=surface.id,
                label=surface.label,
                kind=surface.kind,
                required=surface.required,
                health=(
                    "unknown"
                    if surface.id in FUTURE_SURFACE_IDS
                    else ("ok" if readiness.healthy else "error")
                ),
                credential_reference=readiness.reference,
                message=readiness.message,
                next_step=readiness.next_step,
            )
        )
    ready = all(check.health == "ok" for check in checks if check.required)
    return PublishPreflightResponse(asset_id=asset.asset_id, ready=ready, checks=checks)


def _apply_override(
    surface: PublishSurfaceStatus,
    justification: str,
    at: datetime,
) -> PublishSurfaceStatus:
    return surface.model_copy(
        update={
            "state": "overridden",
            "approval": "overridden",
            "health": "warning",
            "completed_at": at,
            "message": f"{surface.label} was skipped with an audit-logged override.",
            "next_step": "Review the override justification before closing the public record.",
            "override_justification": justification,
        }
    )


def _provider_failure(
    surface: PublishSurfaceStatus, *, at: datetime, exc: Exception
) -> PublishSurfaceStatus:
    """Record a provider delivery failure on the surface (cable-surface pattern).

    Real adapters can fail on the network; one bad surface must not 500 the
    whole approval. The surface stays retryable through the existing
    per-surface retry endpoint.
    """

    return surface.model_copy(
        update={
            "state": "failed",
            "approval": "approved",
            "health": "warning",
            "completed_at": at,
            "message": f"{surface.label} delivery failed; retry this surface.",
            "next_step": str(exc),
        }
    )


# Surfaces approve_publish never pre-checks against the provider registry:
# Portal is gated on manifest_url (not a provider); Podcast uses local
# media and has no external provider; Subscriber
# notifications has real per-channel readiness logic and is checked when
# selected. The Cable file package has its own local
# configuredness check (build_cable_file_package_for_asset) that already
# reports "not_configured" without failing the approval (candidate #17).
_PROVIDER_PRECHECK_EXCLUDED_SURFACE_IDS = {
    "portal",
    CABLE_PACKAGE_SURFACE_ID,
    *FUTURE_SURFACE_IDS,
}


def _blocked_selected_surfaces(
    asset: StaffAssetRow,
    *,
    approved_ids: set[str],
    overrides: dict[str, str],
    registry: ProviderRegistry,
    subscribe_store: SubscribeStore | None,
    subscribe_targets: Sequence[tuple[str, str]],
) -> dict[str, SurfaceReadiness]:
    """Readiness failures among the surfaces the operator actually selected.

    Only surfaces in ``approved_ids`` (and not overridden) are checked (plan
    item 9: a broken UNselected provider must never block portal-only or
    otherwise unrelated publishing). Reads the same registry
    ``build_publish_preflight`` reads (plan item 8), so approval cannot
    disagree with what preflight already told the operator.
    """
    blocked: dict[str, SurfaceReadiness] = {}
    for surface in build_initial_surfaces(asset):
        if surface.id in _PROVIDER_PRECHECK_EXCLUDED_SURFACE_IDS:
            continue
        if surface.id in overrides or surface.id not in approved_ids:
            continue
        readiness = describe_surface_readiness(
            surface.id,
            label=surface.label,
            registry=registry,
            subscribe_store=subscribe_store,
            subscribe_targets=subscribe_targets,
        )
        if readiness is not None and not readiness.healthy:
            blocked[surface.id] = readiness
    return blocked


def approve_publish(
    *,
    asset: StaffAssetRow,
    request: PublishApprovalRequest,
    store: PublishStore,
    registry: ProviderRegistry | None = None,
    media_path: Path | None = None,
    podcast_store: PodcastStore | None = None,
    subscribe_store: SubscribeStore | None = None,
    delivery_store: NotificationDeliveryStore | None = None,
    target_lookup: ChannelAssociationLookup | None = None,
    notification_retry_only: bool = False,
) -> PublishRunRecord:
    """Approve and execute the publish surfaces.

    Archive/reach clients resolve through the provider registry (Stage C):
    the deterministic mocks remain the configured defaults; real adapters are
    selected per kind with ``CIVICCAST_PROVIDER_*`` (Beta B5).

    ``media_path`` is the asset's local recording file when the caller could
    resolve one. Adapters that support full-media publishing
    (``upload_path`` / ``upload_vod_path``) receive it; the mocks keep the
    deterministic verification payload.

    WP-03: before anything executes, every operator-selected surface's
    provider configuration is checked with the exact same readiness source
    preflight uses (:func:`describe_surface_readiness`). A missing/invalid
    selected-real configuration raises :class:`PublishConfigurationError`
    (the router turns this into a controlled 409) before any side effect
    begins -- it never reaches an uncaught ``ProviderConfigurationError``.
    Once past that gate, a *runtime*/network exception from an otherwise
    correctly-configured provider marks only that surface ``failed``
    (retryable via the existing per-surface retry) instead of failing the
    whole approval.

    WP-05: ``delivery_store`` keeps the durable per-recipient notification
    receipts, ``target_lookup`` resolves the asset's channel from the
    schedule/live association, and ``notification_retry_only`` is set by
    :func:`retry_publish_surface` so an explicit retry re-attempts only
    deliveries that already exist and are not yet observed sent.
    """
    at = datetime.now(UTC)
    payload = f"{asset.asset_id}:{asset.title}".encode()
    overrides = {override.surface_id: override.justification for override in request.overrides}
    # F-23 (safety): an omitted selection is the canonical Portal surface
    # only, matching the Publish screen's default. It used to be every
    # surface, which put a closed session on Internet Archive for good.
    approved_ids = (
        set(request.approved_surface_ids)
        if request.approved_surface_ids is not None
        else default_approved_surface_ids(asset)
    )
    resolved_registry = registry if registry is not None else default_registry()
    # One resolution per approval, shared by the readiness pre-check, the
    # podcast channel, and the subscriber fan-out, so all three agree.
    publication_targets = resolve_publication_targets(asset, lookup=target_lookup)
    blocked = _blocked_selected_surfaces(
        asset,
        approved_ids=approved_ids,
        overrides=overrides,
        registry=resolved_registry,
        subscribe_store=subscribe_store,
        subscribe_targets=[
            (target.target_type, target.target_id) for target in publication_targets
        ],
    )
    if blocked:
        raise PublishConfigurationError(blocked)

    _client_cache: dict[str, Any] = {}

    def _client(kind: str) -> Any:
        # Lazy + memoized: only surfaces actually approved this run resolve a
        # client (plan item 9), and local-nas-rsync/local-nas-zfs (or
        # youtube-live/youtube-vod) share one resolved client instead of
        # constructing it twice.
        if kind not in _client_cache:
            _client_cache[kind] = resolved_registry.resolve(kind)
        return _client_cache[kind]

    surfaces: list[PublishSurfaceStatus] = []
    events: list[PublishAuditEvent] = []
    nas_proof_pair: tuple[Any, Any] | None = None
    for surface in build_initial_surfaces(asset):
        if surface.id in overrides:
            updated = _apply_override(surface, overrides[surface.id], at)
            surfaces.append(updated)
            events.append(
                _event(
                    asset_id=asset.asset_id,
                    surface=updated,
                    action="overridden",
                    operator_id=request.operator_id,
                    message=updated.message,
                    at=at,
                )
            )
            continue
        if surface.id not in approved_ids:
            surfaces.append(surface)
            continue
        if surface.id == "portal" and asset.manifest_url:
            updated = surface.model_copy(
                update={
                    "state": "succeeded",
                    "approval": "approved",
                    "health": "ok",
                    "completed_at": at,
                    "message": "Canonical portal is public for this recording.",
                    "next_step": "Monitor archive and reach surfaces independently.",
                }
            )
        elif surface.id == "internet-archive":
            try:
                ia = _client(PROVIDER_KIND_INTERNET_ARCHIVE)
                if media_path is not None and hasattr(ia, "upload_path"):
                    proof = ia.upload_path(asset_id=asset.asset_id, path=media_path)
                else:
                    proof = ia.upload(asset_id=asset.asset_id, payload=payload)
            except Exception as exc:
                updated = _provider_failure(surface, at=at, exc=exc)
            else:
                updated = surface.model_copy(
                    update={
                        "state": "succeeded",
                        "approval": "approved",
                        "health": "ok",
                        "url": proof.target_url_or_path,
                        "verification_hash": proof.verification_hash,
                        "completed_at": at,
                        "simulated": proof.simulated,
                        "message": (
                            "SIMULATED - no item was created at the Internet Archive. "
                            "An admin must set CIVICCAST_PROVIDER_INTERNET_ARCHIVE=real "
                            "with valid credentials before this counts as the legal archive."
                            if proof.simulated
                            else "Internet Archive item URL verifies hash-match."
                        ),
                        "next_step": (
                            "Ask an admin to enable the real Internet Archive provider."
                            if proof.simulated
                            else "Confirm local NAS proofs before archive verification."
                        ),
                    }
                )
        elif surface.id in {"local-nas-rsync", "local-nas-zfs"}:
            try:
                if nas_proof_pair is None:
                    # One archive run per approval: both NAS surfaces share the
                    # same copy+snapshot pair instead of archiving twice.
                    nas = _client(PROVIDER_KIND_LOCAL_NAS)
                    if media_path is not None and hasattr(nas, "archive_path"):
                        nas_proof_pair = nas.archive_path(asset_id=asset.asset_id, path=media_path)
                    else:
                        nas_proof_pair = nas.archive(asset_id=asset.asset_id, payload=payload)
            except Exception as exc:
                updated = _provider_failure(surface, at=at, exc=exc)
            else:
                proof = nas_proof_pair[0 if surface.id == "local-nas-rsync" else 1]
                updated = surface.model_copy(
                    update={
                        "state": "succeeded",
                        "approval": "approved",
                        "health": "ok",
                        "path": proof.target_url_or_path,
                        "verification_hash": proof.verification_hash,
                        "completed_at": at,
                        "simulated": proof.simulated,
                        "message": (
                            f"SIMULATED - {surface.label} did not write any file. "
                            "An admin must set CIVICCAST_PROVIDER_LOCAL_NAS=real before "
                            "this counts as the station's retained copy."
                            if proof.simulated
                            else f"{surface.label} wrote and verified a hash-matched copy."
                        ),
                        "next_step": (
                            "Ask an admin to enable the real local-NAS provider."
                            if proof.simulated
                            else "Keep both local NAS proofs with the archive evidence."
                        ),
                    }
                )
        elif surface.id == "youtube-live":
            previous_run = store.get_run(asset.asset_id)
            previous = (
                next((item for item in previous_run.surfaces if item.id == surface.id), None)
                if previous_run
                else None
            )
            if previous:
                surface = surface.model_copy(
                    update={
                        key: getattr(previous, key)
                        for key in (
                            "broadcast_id",
                            "stream_id",
                            "scheduled_start",
                            "scheduled_end",
                            "channel_id",
                            "url",
                            "delivery_state",
                        )
                    }
                )
            options = {}
            if request.youtube_scheduled_start or (previous and previous.broadcast_id):
                options = {
                    "scheduled_start": request.youtube_scheduled_start,
                    "scheduled_end": request.youtube_scheduled_end,
                    "broadcast_id": previous.broadcast_id if previous else None,
                    "stream_id": previous.stream_id if previous else None,
                }
            try:
                youtube = _client(PROVIDER_KIND_YOUTUBE)
                youtube_live_proof = youtube.publish_live(
                    asset_id=asset.asset_id,
                    title=asset.title,
                    channel_id=request.youtube_channel_id,
                    **options,
                )
            except Exception as exc:
                updated = _provider_failure(surface, at=at, exc=exc)
            else:
                updated = surface.model_copy(
                    update={
                        "state": "running"
                        if youtube_live_proof.delivery_state
                        in {"scheduled", "configured", "starting"}
                        else (
                            "succeeded"
                            if youtube_live_proof.delivery_state == "live"
                            else (
                                "failed"
                                if youtube_live_proof.delivery_state == "failed"
                                else "unverified"
                            )
                        ),
                        "approval": "approved",
                        "health": "ok"
                        if youtube_live_proof.delivery_state == "live"
                        else "warning",
                        "url": youtube_live_proof.url,
                        "completed_at": at if youtube_live_proof.delivery_state == "live" else None,
                        "simulated": youtube_live_proof.delivery_state == "simulated",
                        "delivery_state": youtube_live_proof.delivery_state,
                        "channel_id": youtube_live_proof.channel_id,
                        **{
                            key: getattr(youtube_live_proof, key)
                            for key in (
                                "broadcast_id",
                                "stream_id",
                                "scheduled_start",
                                "scheduled_end",
                            )
                        },
                        "message": f"YouTube Live: {youtube_live_proof.delivery_state}. Only destination-confirmed live output counts as delivery.",
                        "next_step": "Treat this as reach evidence, not the system of record.",
                    }
                )
        elif surface.id == "youtube-vod":
            try:
                youtube = _client(PROVIDER_KIND_YOUTUBE)
                if media_path is not None and hasattr(youtube, "upload_vod_path"):
                    youtube_vod_proof = youtube.upload_vod_path(
                        asset_id=asset.asset_id, path=media_path
                    )
                else:
                    youtube_vod_proof = youtube.upload_vod(asset_id=asset.asset_id)
            except Exception as exc:
                updated = _provider_failure(surface, at=at, exc=exc)
            else:
                updated = surface.model_copy(
                    update={
                        "state": "unverified"
                        if youtube_vod_proof.delivery_state == "simulated"
                        else "succeeded",
                        "approval": "approved",
                        "health": "ok",
                        "simulated": youtube_vod_proof.delivery_state == "simulated",
                        "url": youtube_vod_proof.url,
                        "completed_at": at,
                        "message": "Simulated YouTube VOD; nothing was uploaded."
                        if youtube_vod_proof.delivery_state == "simulated"
                        else "YouTube VOD upload confirmed.",
                        "next_step": "Treat this as reach evidence, not the system of record.",
                    }
                )
        elif surface.id == "podcast":
            podcast_channel_id = next(
                (
                    target.target_id
                    for target in publication_targets
                    if target.target_type == "channel"
                ),
                DEFAULT_CHANNEL_ID_FALLBACK,
            )
            if asset.published_at is None:
                podcast_error = (
                    "Publish this recording to Portal first, then retry Podcast "
                    "after the public record is confirmed."
                )
            elif media_path is None or podcast_store is None:
                podcast_error = "A local recording and podcast storage are required."
            else:
                try:
                    episode = create_podcast_episode(
                        PodcastEpisodeCreate(
                            asset_id=asset.asset_id,
                            channel_id=podcast_channel_id,
                            title=asset.title,
                            portal_url=asset.manifest_url or "",
                            source_media_url=str(media_path.resolve()),
                            summary=f"Audio from {asset.title}.",
                        )
                    )
                    podcast_store.upsert_episode(episode)
                except (PodcastMediaError, OSError) as exc:
                    podcast_error = str(exc)
                except Exception as exc:
                    podcast_error = (
                        f"Podcast conversion or storage failed ({type(exc).__name__}). "
                        "Check station storage and retry."
                    )
                else:
                    podcast_error = ""
            if podcast_error:
                updated = surface.model_copy(
                    update={
                        "state": "failed",
                        "approval": "approved",
                        "health": "warning",
                        "completed_at": at,
                        "message": "Podcast audio was not published.",
                        "next_step": podcast_error,
                    }
                )
            else:
                base_url = os.environ["CIVICCAST_PUBLIC_BASE_URL"].rstrip("/")
                updated = surface.model_copy(
                    update={
                        "state": "succeeded",
                        "approval": "approved",
                        "health": "ok",
                        "url": f"{base_url}/api/public/podcast/{episode.channel_id}.xml",
                        "completed_at": at,
                        "message": "Podcast audio was created and added to the RSS feed.",
                        "next_step": "Open the feed to check the episode and audio.",
                    }
                )
        elif surface.id == "subscriber-notifications":
            if asset.published_at is None:
                updated = surface.model_copy(
                    update={
                        "state": "pending",
                        "approval": "approved",
                        "health": "warning",
                        "message": "Subscriber notices are waiting for the recording to become public.",
                        "next_step": "Publish the recording to Portal; notices will send after visibility is confirmed.",
                    }
                )
                surfaces.append(updated)
                continue
            # WP-05: this branch used to build a payload, mark the surface
            # "succeeded" and stop -- no subscriber was ever contacted. It now
            # runs the real fan-out through the existing SMTP/webhook adapters
            # and reports the state its durable delivery receipts show.
            outcome = deliver_publication_notifications(
                asset_id=asset.asset_id,
                title=asset.title,
                published_at=at,
                targets=publication_targets,
                manifest_url=asset.manifest_url,
                subscribe_store=subscribe_store,
                delivery_store=delivery_store,
                registry=resolved_registry,
                retry_only=notification_retry_only,
            )
            updated = surface.model_copy(
                update={
                    "state": outcome.state,
                    "approval": "approved",
                    "health": outcome.health,
                    "completed_at": at,
                    "message": outcome.message,
                    "next_step": outcome.next_step,
                    # Badged exactly like a simulated Internet Archive or
                    # YouTube surface: a mock adapter accepted the notice and
                    # nobody received it (GauntletGate TW-1).
                    "simulated": outcome.simulated,
                    "notification_summary": outcome.summary,
                }
            )
        elif surface.id == CABLE_PACKAGE_SURFACE_ID:
            try:
                cable_package = build_cable_file_package_for_asset(asset)
            except CablePackageNotConfiguredError as exc:
                # Field evidence (candidate #17): an operator saw a red
                # "failed: Cable file package was not created" on an
                # otherwise-successful publish, for a PEG/headend handoff
                # surface most stations never configure at all -- "not set
                # up" and "we tried and it broke" were indistinguishable.
                # This surface was never attempted for real, so it is not a
                # failure: "not_configured" (already a valid
                # PublishSurfaceStateValue) plus a neutral "info" health
                # keeps the row from reading as red/broken.
                updated = surface.model_copy(
                    update={
                        "state": "not_configured",
                        "approval": "approved",
                        "health": "unknown",
                        "completed_at": at,
                        "message": "Cable file package is not set up (optional).",
                        "next_step": str(exc),
                    }
                )
            except CablePackageError as exc:
                updated = surface.model_copy(
                    update={
                        "state": "failed",
                        "approval": "approved",
                        "health": "warning",
                        "completed_at": at,
                        "message": "Cable file package was not created.",
                        "next_step": str(exc),
                    }
                )
            else:
                updated = surface.model_copy(
                    update={
                        "state": "succeeded",
                        "approval": "approved",
                        "health": "ok",
                        "path": str(cable_package.zip_path),
                        "verification_hash": cable_package.verification_hash,
                        "completed_at": at,
                        "message": "Cable file package ZIP and SHA-256 proof were created.",
                        "next_step": cable_package.next_step,
                    }
                )
        else:
            updated = surface
        surfaces.append(updated)
        if updated.state == "succeeded":
            events.append(
                _event(
                    asset_id=asset.asset_id,
                    surface=updated,
                    action="succeeded",
                    operator_id=request.operator_id,
                    message=updated.message,
                    at=at,
                )
            )

    record = PublishRunRecord(
        asset_id=asset.asset_id,
        operator_id=request.operator_id,
        operator_display_name=request.operator_display_name,
        approved_at=at,
        surfaces=surfaces,
        audit_events=events,
    )
    return store.upsert_run(record)


def send_pending_notifications_after_publication(
    *,
    asset: StaffAssetRow,
    record: PublishRunRecord,
    store: PublishStore,
    subscribe_store: SubscribeStore,
    delivery_store: NotificationDeliveryStore | None,
    registry: ProviderRegistry,
    target_lookup: ChannelAssociationLookup | None,
) -> PublishRunRecord:
    """Send approved notices only after the canonical public write succeeds."""
    if asset.published_at is None:
        return record
    waiting = next((s for s in record.surfaces if s.id == "subscriber-notifications"), None)
    if (
        waiting is None
        or waiting.approval != "approved"
        or waiting.state != "pending"
        or waiting.notification_summary is not None
    ):
        return record
    outcome = deliver_publication_notifications(
        asset_id=asset.asset_id,
        title=asset.title,
        published_at=asset.published_at,
        targets=resolve_publication_targets(asset, lookup=target_lookup),
        manifest_url=asset.manifest_url,
        subscribe_store=subscribe_store,
        delivery_store=delivery_store,
        registry=registry,
    )
    finished = waiting.model_copy(
        update={
            "state": outcome.state,
            "health": outcome.health,
            "completed_at": datetime.now(UTC),
            "message": outcome.message,
            "next_step": outcome.next_step,
            "simulated": outcome.simulated,
            "notification_summary": outcome.summary,
        }
    )
    return store.upsert_run(
        record.model_copy(
            update={"surfaces": [finished if s.id == finished.id else s for s in record.surfaces]}
        )
    )


def retry_publish_surface(
    *,
    asset: StaffAssetRow,
    surface_id: str,
    request: PublishRetryRequest,
    store: PublishStore,
    registry: ProviderRegistry | None = None,
    media_path: Path | None = None,
    podcast_store: PodcastStore | None = None,
    subscribe_store: SubscribeStore | None = None,
    delivery_store: NotificationDeliveryStore | None = None,
    target_lookup: ChannelAssociationLookup | None = None,
) -> PublishRunRecord:
    """Retry one surface while preserving the rest of the publish run.

    A retry is a one-surface approval (plan items 5/8 apply here too): if the
    retried surface's provider configuration is missing/invalid,
    ``approve_publish`` raises :class:`PublishConfigurationError` and this
    function does not catch it -- the caller (the retry route) turns it into
    the same controlled 409 the approve route uses, rather than silently
    "retrying" a surface that was never going to succeed.
    """
    known_surface_ids = {surface.id for surface in build_initial_surfaces(asset)}
    if surface_id not in known_surface_ids:
        raise ValueError(f"Unknown publish surface: {surface_id}")
    previous = store.get_run(asset.asset_id)
    previous_surfaces = previous.surfaces if previous is not None else build_initial_surfaces(asset)
    previous_by_id = {surface.id: surface for surface in previous_surfaces}
    retried = approve_publish(
        asset=asset,
        request=PublishApprovalRequest(
            operator_id=request.operator_id,
            operator_display_name=request.operator_display_name,
            approved_surface_ids=[surface_id],
            youtube_scheduled_start=request.youtube_scheduled_start
            if "youtube_scheduled_start" in request.model_fields_set
            else next((s.scheduled_start for s in previous_surfaces if s.id == surface_id), None),
            youtube_scheduled_end=request.youtube_scheduled_end
            if "youtube_scheduled_end" in request.model_fields_set
            else next((s.scheduled_end for s in previous_surfaces if s.id == surface_id), None),
            youtube_channel_id=request.youtube_channel_id
            or next((s.channel_id for s in previous_surfaces if s.id == surface_id), None),
        ),
        store=InMemoryPublishStoreProxy(previous),
        registry=registry,
        media_path=media_path,
        podcast_store=podcast_store,
        subscribe_store=subscribe_store,
        delivery_store=delivery_store,
        target_lookup=target_lookup,
        # WP-05 plan item 6: an explicit retry re-attempts only deliveries that
        # already exist and are failed or stale pending. Queued webhooks stay
        # with the background worker. It never contacts a
        # recipient already observed sent, and never starts a delivery the
        # original run did not intend.
        notification_retry_only=True,
    )
    updated_surface = next(surface for surface in retried.surfaces if surface.id == surface_id)
    old_retry_count = previous_by_id.get(surface_id, updated_surface).retry_count
    updated_surface = updated_surface.model_copy(
        update={"retry_count": old_retry_count + 1, "last_attempt_at": datetime.now(UTC)}
    )
    merged = [
        updated_surface if surface.id == surface_id else surface for surface in previous_surfaces
    ]
    events = list(previous.audit_events if previous is not None else [])
    events.append(
        _event(
            asset_id=asset.asset_id,
            surface=updated_surface,
            action="retried",
            operator_id=request.operator_id,
            message=f"{updated_surface.label} was retried by the operator.",
            at=datetime.now(UTC),
        )
    )
    events.extend(retried.audit_events)
    return store.upsert_run(
        PublishRunRecord(
            asset_id=asset.asset_id,
            operator_id=request.operator_id,
            operator_display_name=request.operator_display_name,
            approved_at=previous.approved_at if previous is not None else retried.approved_at,
            surfaces=merged,
            audit_events=events,
        )
    )


class InMemoryPublishStoreProxy:
    """Tiny throwaway store so retry can reuse the single-surface executor."""

    def __init__(self, previous: PublishRunRecord | None) -> None:
        self._previous = previous
        self._record: PublishRunRecord | None = None

    def get_run(self, asset_id: str) -> PublishRunRecord | None:
        if self._previous is not None and self._previous.asset_id == asset_id:
            return self._previous
        return None

    def get_runs(self, asset_ids: Sequence[str]) -> dict[str, PublishRunRecord]:
        found = {}
        for asset_id in asset_ids:
            record = self.get_run(asset_id)
            if record is not None:
                found[asset_id] = record
        return found

    def upsert_run(self, record: PublishRunRecord) -> PublishRunRecord:
        self._record = record
        return record

    def replace_surfaces(
        self,
        asset_id: str,
        expected: list[PublishSurfaceStatus],
        updated: list[PublishSurfaceStatus],
    ) -> bool:
        run = self._record or self.get_run(asset_id)
        if run is None or run.asset_id != asset_id or run.surfaces != expected:
            return False
        self._record = run.model_copy(update={"surfaces": updated})
        return True


#: Reach/audience states that mean "this did not fully reach its audience".
_DEGRADED_REACH_STATES = frozenset({"failed", "partial", "unverified"})


def _normalized_notification_surface(surface: PublishSurfaceStatus) -> PublishSurfaceStatus:
    """Downgrade a receipt-less historical ``succeeded`` notice row to ``unverified``.

    Every ``subscriber-notifications`` row written before WP-05 says
    ``succeeded`` because the old code marked it so after merely building a
    payload -- no notice was sent and no receipt exists. Those rows are real
    history, so they are not rewritten in the database; they are *presented*
    as ``unverified`` (plan item 10) so a green pill can never stand in for
    delivery that cannot be shown to have happened.
    """

    if surface.id != "subscriber-notifications":
        return surface
    if surface.state != "succeeded" or surface.notification_summary is not None:
        return surface
    return surface.model_copy(
        update={
            "state": "unverified",
            "health": "warning",
            "message": (
                "This publish run recorded subscriber notices as sent, but it kept no "
                "delivery receipt, so the station cannot show that anyone was notified."
            ),
            "next_step": (
                "Retry the subscriber notifications surface to send and record real "
                "deliveries for this recording."
            ),
        }
    )


def _dashboard_state(
    *,
    surfaces: list[PublishSurfaceStatus],
    public_record_required: bool,
) -> tuple[PublishDashboardStateValue, str]:
    portal = next(surface for surface in surfaces if surface.id == "portal")
    archive = [surface for surface in surfaces if surface.kind == "archive" and surface.required]
    reach = [surface for surface in surfaces if surface.kind in {"reach", "audience"}]
    required_failed = any(surface.required and surface.state == "failed" for surface in surfaces)
    pending_required = any(
        surface.required and surface.state in {"pending", "running"} for surface in surfaces
    )
    archive_verified = bool(archive) and all(
        surface.state in {"succeeded", "overridden"} for surface in archive
    )
    # WP-05: a fan-out surface that only partly delivered, or whose success has
    # no receipt behind it, is degraded reach -- not "Complete". Rolling either
    # into the green path is precisely the lie this work package removes.
    reach_failed = any(surface.state in _DEGRADED_REACH_STATES for surface in reach)

    if portal.state == "blocked":
        return "preflight_blocked", "Preflight blocked"
    if required_failed:
        return "failed_needs_action", "Failed - needs action"
    if portal.state in {"pending", "running"}:
        return "draft", "Draft"
    if portal.state == "succeeded" and pending_required:
        return "archive_pending", "Archive pending"
    if portal.state == "succeeded" and reach_failed:
        return "reach_degraded", "Reach degraded"
    if public_record_required and archive_verified:
        return "archive_verified", "Archive verified"
    if portal.state == "succeeded":
        return "complete", "Complete"
    return "publishing", "Publishing"


def _normalized_youtube_surface(surface: PublishSurfaceStatus) -> PublishSurfaceStatus:
    if surface.id not in {"youtube-live", "youtube-vod"} or surface.delivery_state is not None:
        return surface
    # Historical success rows predate destination confirmation. Ingest URLs are
    # private encoder configuration, never links to display to operators.
    update: dict[str, Any] = {}
    if surface.url and surface.url.lower().startswith(("rtmp://", "rtmps://")):
        update["url"] = None
    if surface.state == "succeeded":
        update.update(
            state="unverified",
            health="warning",
            message="Historical YouTube result has no destination delivery confirmation.",
            next_step="Inspect YouTube Studio and retry if needed.",
        )
    return surface.model_copy(update=update) if update else surface


def build_publish_asset_status(
    asset: StaffAssetRow,
    record: PublishRunRecord | None = None,
) -> PublishAssetStatus:
    public_record_required = _is_public_record(asset)
    surfaces = [
        # Visibility is authoritative; retain the durable delivery history when
        # an operator removes an asset from the portal.
        _blocked_portal_surface(asset)
        if surface.id == "portal" and surface.state == "succeeded" and asset.published_at is None
        else _normalized_notification_surface(_normalized_youtube_surface(surface))
        for surface in (record.surfaces if record is not None else build_initial_surfaces(asset))
    ]
    dashboard_state, dashboard_label = _dashboard_state(
        surfaces=surfaces,
        public_record_required=public_record_required,
    )
    canonical_public = any(
        surface.id == "portal" and surface.state == "succeeded" for surface in surfaces
    )
    archive_surfaces = [
        surface for surface in surfaces if surface.kind == "archive" and surface.required
    ]
    archive_verified = bool(archive_surfaces) and all(
        surface.state in {"succeeded", "overridden"} for surface in archive_surfaces
    )
    reach_degraded = canonical_public and any(
        surface.kind in {"reach", "audience"} and surface.state in _DEGRADED_REACH_STATES
        for surface in surfaces
    )
    needs_operator_action = any(
        surface.required and surface.state in {"blocked", "failed"} for surface in surfaces
    )
    return PublishAssetStatus(
        asset_id=asset.asset_id,
        title=asset.title,
        dashboard_state=dashboard_state,
        dashboard_label=dashboard_label,
        canonical_public=canonical_public,
        archive_verified=archive_verified,
        reach_degraded=reach_degraded,
        needs_operator_action=needs_operator_action,
        public_record_required=public_record_required,
        published_at=asset.published_at,
        surfaces=surfaces,
    )


def build_publish_dashboard(
    assets: list[StaffAssetRow],
    store: PublishStore | None = None,
    *,
    transform_run: Callable[[PublishRunRecord], PublishRunRecord] | None = None,
) -> PublishDashboardResponse:
    # One batched fetch, not one query per asset. This list is unbounded (it is
    # every meeting the station has ever recorded), so a per-row lookup made the
    # dashboard's cost grow forever -- GauntletGate PE-1.
    # One batched fetch, not one query per asset. This list is unbounded (it is
    # every meeting the station has ever recorded), so a per-row lookup made the
    # dashboard's cost grow forever -- GauntletGate PE-1.
    runs = {} if store is None else store.get_runs([asset.asset_id for asset in assets])
    if transform_run is not None:
        runs = {asset_id: transform_run(run) for asset_id, run in runs.items()}
    rows = [build_publish_asset_status(asset, runs.get(asset.asset_id)) for asset in assets]
    summary = PublishDashboardSummary(
        total_assets=len(rows),
        draft=sum(1 for row in rows if row.dashboard_state == "draft"),
        portal_live=sum(1 for row in rows if row.canonical_public),
        archive_verified=sum(1 for row in rows if row.archive_verified),
        degraded=sum(1 for row in rows if row.reach_degraded),
        needs_operator_action=sum(1 for row in rows if row.needs_operator_action),
    )
    return PublishDashboardResponse(summary=summary, assets=rows)
