# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI router for the operator publish dashboard."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from civiccast.activitypub.remote import ActivityPubDeliveryClient, ActivityPubRemoteError
from civiccast.activitypub.service import deliver_publish_activity, record_publish_activity
from civiccast.activitypub.store import ActivityPubStore
from civiccast.auth.roles import require_any_role
from civiccast.captions.settings import RecordedCaptionSettings
from civiccast.captions.settings_router import get_recorded_caption_settings
from civiccast.captions.vod_job import OfflineCaptionJobStore, enqueue_offline_caption_job
from civiccast.live.recording_paths import local_recording_path
from civiccast.live.router import get_live_finalization_worker
from civiccast.platform.broker import BrokerClient, get_broker_client
from civiccast.platform.providers import ProviderRegistry, default_registry
from civiccast.platform.stores import resolve_app_store
from civiccast.podcast.router import get_podcast_store
from civiccast.podcast.store import PodcastStore
from civiccast.publish.models import (
    PublishApprovalRequest,
    PublishAssetStatus,
    PublishDashboardResponse,
    PublishPreflightResponse,
    PublishRetryRequest,
    PublishRunRecord,
)
from civiccast.publish.service import (
    PublishConfigurationError,
    approve_publish,
    build_publish_approved_event,
    build_publish_asset_status,
    build_publish_dashboard,
    build_publish_preflight,
    retry_publish_surface,
    send_pending_notifications_after_publication,
)
from civiccast.publish.store import PublishStore
from civiccast.publish.targets import ChannelAssociationLookup
from civiccast.schedule.models import StaffAssetRow
from civiccast.schedule.paths import resolve_vod_package_dir
from civiccast.schedule.router import get_postgres_store
from civiccast.subscribe.outcome_store import NotificationDeliveryStore
from civiccast.subscribe.router import (
    get_notification_delivery_store,
    get_publication_target_lookup,
    get_subscribe_store,
)
from civiccast.subscribe.store import SubscribeStore

_LOG = logging.getLogger(__name__)

staff_router = APIRouter(prefix="/api/staff/publish", tags=["staff", "publish"])


def _resolve_local_recording(finalization_worker: Any, asset_id: str) -> Path | None:
    """Resolve the asset's local recording file for full-media publishing.

    Live recordings share their asset_id with the live session, so the
    finalization job's ``recording_uri`` names the local file (Beta B5).
    Returns None for non-live assets, unwired workers (ephemeral mode), or a
    missing file — callers fall back to the deterministic verification
    payload.
    """

    if finalization_worker is None:
        return None
    try:
        job_status = finalization_worker.get_status(asset_id)
    except Exception:
        return None
    if job_status is None or not job_status.recording_uri:
        return None
    path = local_recording_path(job_status.recording_uri)
    if path is None or not path.exists():
        return None
    return path


def _resolve_publish_media(finalization_worker: Any, asset: StaffAssetRow) -> Path | None:
    live_recording = _resolve_local_recording(finalization_worker, asset.asset_id)
    if live_recording is not None:
        return live_recording
    if asset.file_path:
        candidate = Path(asset.file_path)
        if candidate.is_file():
            return candidate
    return None


_DB_NOT_READY_DESCRIPTION = "Durable storage not ready -- run Setup storage or set DATABASE_URL"
_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)


def _apply_portal_visibility(
    *,
    asset_store: Any,
    publish_store: PublishStore,
    asset_id: str,
    staff_asset: StaffAssetRow,
    record: PublishRunRecord,
) -> tuple[StaffAssetRow, PublishRunRecord]:
    """Make Portal visibility and its recorded surface outcome agree.

    Provider surfaces may succeed independently, but Portal is not reported as
    successful unless the canonical asset row actually becomes public. A
    failed visibility write is persisted as a retryable Portal failure while
    the resident asset remains private.
    """
    portal = next((surface for surface in record.surfaces if surface.id == "portal"), None)
    if portal is None or portal.state != "succeeded":
        return staff_asset, record
    try:
        published = cast(
            StaffAssetRow,
            asset_store.mark_published(asset_id, published_at=datetime.now(UTC)),
        )
    except Exception:
        failed_portal = portal.model_copy(
            update={
                "state": "failed",
                "health": "warning",
                "message": "Portal publication could not make the recording public.",
                "next_step": (
                    "The recording remains private. Check durable storage, then retry "
                    "the Portal surface."
                ),
            }
        )
        corrected = record.model_copy(
            update={
                "surfaces": [
                    failed_portal if surface.id == "portal" else surface
                    for surface in record.surfaces
                ]
            }
        )
        return staff_asset, publish_store.upsert_run(corrected)
    return published, record


def get_publish_store(request: Request) -> PublishStore:
    return cast(PublishStore, resolve_app_store(request, "publish_store", surface="Publish store"))


def get_provider_registry(request: Request) -> ProviderRegistry:
    """Resolve the app's single provider registry (WP-03 plan items 1 and 8).

    Preflight and approval both depend on this so they read the exact same
    registry within one process -- they cannot disagree about whether a
    surface's real-provider configuration is valid. Falls back to
    ``default_registry()`` for an app instance that never wired
    ``app.state.provider_registry`` (this never fails: registering the
    shipped mock/real factories is itself side-effect-free).
    """

    return cast(
        ProviderRegistry,
        getattr(request.app.state, "provider_registry", None) or default_registry(),
    )


def get_caption_job_store(request: Request) -> OfflineCaptionJobStore | None:
    """Resolve the offline caption job queue, or ``None`` when unavailable."""

    return cast(
        "OfflineCaptionJobStore | None",
        resolve_app_store(request, "caption_job_store", surface="Offline caption job store"),
    )


class CaptionJobNotQueueableError(Exception):
    """A recording's caption job could not be queued.

    Carries the operator-facing *cause* clause only; the route wraps it in
    :data:`CAPTION_JOB_UNQUEUEABLE_DETAIL`, which supplies the rest of the
    sentence and the reassurance that nothing was published.
    """


#: Raised as the 409 body when a recording cannot have its caption job
#: queued. Publication is what starts the caption obligation, so approving
#: publication while knowing the obligation cannot even be *recorded* is the
#: one outcome this route must not produce.
CAPTION_JOB_UNQUEUEABLE_DETAIL = (
    "Publish blocked: CivicCast cannot queue this recording's caption job, so approving "
    "would put it on the public record with no path to the station's configured captions. "
    "Nothing was published. Cause: {cause} Fix that and approve again."
)


def _resolve_caption_package_dir(
    asset_id: str,
    finalization_worker: Any,
) -> Path | None:
    """Resolve where this asset's HLS package actually lives.

    Two packaging conventions exist and only one of them is under the upload
    root, so asking ``resolve_vod_package_dir`` alone gets a LIVE-finalized
    recording wrong:

    * **live-finalized** -- ``LiveFinalizationWorker._package_once`` writes to
      ``<recording_path.parent>/<live_session_id>-hls/`` and records the
      manifest on the finalization job. Nothing about it is under
      ``CIVICCAST_UPLOAD_DIR``, and a station broadcasting live may not have
      an upload root configured at all.
    * **uploaded** -- ``.civiccast-packages/<asset_id>`` under the upload root.

    Live is checked first, through the same ``finalization_worker.get_status``
    seam :func:`_resolve_local_recording` already uses. This closes the "a
    live-finalized recording would transcribe but fail to attach" follow-up in
    docs/ops/background-workers.md -- and, now that a missing package
    directory blocks approval rather than merely failing stage two later, it
    is what keeps a live station with no upload root from being told it cannot
    publish.

    **How far the agreement with the media-serving path actually goes.** The
    *live-finalized precedence* matches
    ``civiccast.stream.media_router._package_dir_for_asset``: the finalization
    job's manifest path wins there too. The upload branch does NOT match. That
    function additionally checks the asset's ``manifest_url`` suffix, verifies
    the resolved directory is contained by the package root and the package
    root by the upload root, and requires ``playlist.m3u8`` to exist -- and,
    failing all that, falls back to the legacy pre-rc14 shared
    ``<file_path>/hls`` location. This resolver does none of that: it returns
    the standard ``.civiccast-packages/<asset_id>`` path from
    ``resolve_vod_package_dir`` without existence or containment checks.

    Known gap, stated rather than papered over: a **legacy pre-rc14 package**
    living at ``<file_path>/hls`` is not resolved here, so the caption gate
    would refuse to queue -- and therefore block publish -- for such an asset
    even though the media router can still serve it. The blast radius is
    stations upgraded across rc14 that still hold pre-rc14 packages; new
    packaging never writes that path. Close it by resolving through a shared
    helper if that population turns out to matter.
    """

    if finalization_worker is not None:
        try:
            job_status = finalization_worker.get_status(asset_id)
        except Exception:  # pragma: no cover - unwired/ephemeral worker
            job_status = None
        manifest_path = getattr(job_status, "local_package_manifest_path", None)
        if manifest_path:
            return Path(manifest_path).resolve().parent
    return resolve_vod_package_dir(asset_id)


def _queue_offline_captions(
    caption_job_store: OfflineCaptionJobStore | None,
    staff_asset: StaffAssetRow,
    *,
    finalization_worker: Any = None,
    caption_settings: RecordedCaptionSettings | None = None,
    require_published: bool = True,
) -> None:
    """Queue captioning for a recording that is becoming public (K3).

    When recorded captions are enabled, publishing queues the job using the
    station's staged caption model and
    files every cue in the operator review queue; nothing reaches the
    resident-facing package until an operator has decided on it.

    **Raises** :class:`CaptionJobNotQueueableError` when the job cannot be
    queued. It used to swallow every failure and log, on the reasoning that
    captioning trails publication and must not fail the publish. The half of
    that reasoning which is still true — captioning trails publication, and a
    public record must not wait days for caption review — is preserved by
    when this runs, not by ignoring its result: the approval route calls it
    *before* approving, so the operator gets a controlled 409 naming the
    cause and nothing is published, rather than a public recording with no
    caption job and only a line in a log file to say so.

    ``require_published=False`` is that pre-approval call: the asset has not
    been marked public yet, so the ``published_at`` check must not veto it.
    The trade is explicit — if the approval that follows then fails, the
    asset has a queued caption job it did not need yet. That is bounded and
    harmless: the job is idempotent per asset (so the operator's retry reuses
    it rather than transcribing twice), and the worker publishes nothing on
    its own — it fills the review queue and waits for an operator either way.
    A public recording with no caption job is not similarly recoverable,
    because nothing afterwards goes looking for one.
    """

    if caption_settings is not None and not caption_settings.enabled:
        _LOG.info(
            "Skipping offline caption enqueue for %s: recorded captions are disabled in station settings.",
            staff_asset.asset_id,
        )
        return
    if require_published and staff_asset.published_at is None:
        return
    source_path = Path(staff_asset.file_path) if staff_asset.file_path else None
    if source_path is None:
        # Not caption-eligible: there is no local recording to transcribe, so
        # no caption job is owed and none can be queued. This is the one skip
        # that stays a skip -- blocking here would stop an operator publishing
        # an asset CivicCast never had the media for, which is a different
        # thing entirely from a recording whose captions cannot be arranged.
        _LOG.info(
            "No offline caption job for %s: the asset has no local recording file.",
            staff_asset.asset_id,
        )
        return
    if caption_job_store is None:
        raise CaptionJobNotQueueableError(
            "this station has no caption job store configured, which usually means "
            "durable storage is unavailable — check Settings > System health."
        )
    # Live-finalized packages are NOT under the upload root; see
    # _resolve_caption_package_dir, which mirrors media_router's precedence.
    package_dir = _resolve_caption_package_dir(staff_asset.asset_id, finalization_worker)
    if package_dir is None:
        raise CaptionJobNotQueueableError(
            "CivicCast cannot work out where this recording's packaged video lives, so "
            "there is nowhere to write its caption track. A live recording resolves this "
            "from its finalization job; an uploaded one needs the media storage location "
            "set in Setup."
        )
    try:
        enqueue_offline_caption_job(
            caption_job_store,
            asset_id=staff_asset.asset_id,
            source_path=source_path,
            package_dir=package_dir,
        )
    except Exception as exc:
        _LOG.exception("Could not queue offline captions for %s.", staff_asset.asset_id)
        raise CaptionJobNotQueueableError(f"queueing the caption job failed ({exc}).") from exc


def get_activitypub_store(request: Request) -> ActivityPubStore:
    return cast(
        ActivityPubStore,
        resolve_app_store(request, "activitypub_store", surface="ActivityPub store"),
    )


def get_activitypub_delivery_client(request: Request) -> ActivityPubDeliveryClient:
    return cast(ActivityPubDeliveryClient, request.app.state.activitypub_delivery_client)


def _require_asset_store(postgres_store: Any) -> Any:
    if postgres_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_DB_NOT_READY_DETAIL,
        )
    return postgres_store


@staff_router.get(
    "/assets",
    response_model=PublishDashboardResponse,
    summary="List publish-dashboard status for every asset",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_publish_assets(
    request: Request,
    postgres_store: Any = Depends(get_postgres_store),
    publish_store: PublishStore = Depends(get_publish_store),
) -> PublishDashboardResponse:
    """Return per-asset three-tier publish status for the operator dashboard."""
    asset_store = _require_asset_store(postgres_store)
    assets = cast(list[StaffAssetRow], asset_store.list_all())
    return build_publish_dashboard(
        assets,
        publish_store,
        transform_run=lambda run: _mark_interrupted_youtube(request, publish_store, run),
    )


@staff_router.get(
    "/assets/{asset_id}",
    response_model=PublishAssetStatus,
    summary="Get publish-dashboard status for one asset",
    responses={
        404: {"description": "Asset not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_publish_asset(
    asset_id: str,
    request: Request,
    postgres_store: Any = Depends(get_postgres_store),
    publish_store: PublishStore = Depends(get_publish_store),
) -> PublishAssetStatus:
    """Return three-tier publish status for a single asset."""
    asset_store = _require_asset_store(postgres_store)
    asset = asset_store.get_staff_row(asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )
    run = publish_store.get_run(asset_id)
    if run is not None:
        run = _mark_interrupted_youtube(request, publish_store, run)
    return build_publish_asset_status(cast(StaffAssetRow, asset), run)


def _mark_interrupted_youtube(
    request: Request, store: PublishStore, run: PublishRunRecord
) -> PublishRunRecord:
    output = getattr(request.app.state, "youtube_live_output", None)
    if output is not None and output.knows(run.asset_id):
        return run
    changed = False
    surfaces = []
    for surface in run.surfaces:
        if surface.id == "youtube-live" and surface.delivery_state in {
            "scheduled",
            "configured",
            "starting",
            "live",
        }:
            surface = surface.model_copy(
                update={
                    "state": "failed",
                    "health": "error",
                    "delivery_state": "failed",
                    "message": "YouTube output was interrupted. Check YouTube Studio before retrying.",
                    "next_step": "Confirm the old event has ended, then retry YouTube Live.",
                }
            )
            changed = True
        surfaces.append(surface)
    if changed and store.replace_surfaces(run.asset_id, run.surfaces, surfaces):
        return run.model_copy(update={"surfaces": surfaces})
    return run


@staff_router.post(
    "/assets/{asset_id}/youtube/stop",
    dependencies=[Depends(require_any_role("publish_operator"))],
)
def stop_youtube_output(asset_id: str, request: Request) -> dict[str, str]:
    output = getattr(request.app.state, "youtube_live_output", None)
    try:
        proof = output.stop(asset_id) if output is not None else None
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="No owned YouTube output exists for this recording."
        ) from exc
    if proof is None:
        raise HTTPException(status_code=503, detail="YouTube output is unavailable.")
    return {"state": proof.delivery_state}


@staff_router.get(
    "/assets/{asset_id}/preflight",
    response_model=PublishPreflightResponse,
    summary="Check v0.7 publish readiness before operator approval",
    responses={
        404: {"description": "Asset not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_publish_preflight(
    asset_id: str,
    postgres_store: Any = Depends(get_postgres_store),
    provider_registry: ProviderRegistry = Depends(get_provider_registry),
    subscribe_store: SubscribeStore = Depends(get_subscribe_store),
    target_lookup: ChannelAssociationLookup | None = Depends(get_publication_target_lookup),
) -> PublishPreflightResponse:
    """Return portal, Internet Archive, NAS, YouTube, and subscriber readiness.

    Reads through the real provider registry (WP-03): every check here is the
    same non-secret, side-effect-free readiness ``approve_publish`` uses, so
    this response and what approval would actually do cannot disagree.
    """
    asset_store = _require_asset_store(postgres_store)
    asset = asset_store.get_staff_row(asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )
    return build_publish_preflight(
        cast(StaffAssetRow, asset),
        registry=provider_registry,
        subscribe_store=subscribe_store,
        target_lookup=target_lookup,
    )


@staff_router.post(
    "/assets/{asset_id}/approve",
    response_model=PublishAssetStatus,
    summary="Approve and run the v0.7 three-tier publish workflow",
    dependencies=[Depends(require_any_role("publish_operator"))],
    responses={
        404: {"description": "Asset not found"},
        409: {"description": "Publish preflight blocked"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def approve_publish_asset(
    asset_id: str,
    request: PublishApprovalRequest,
    http_request: Request,
    postgres_store: Any = Depends(get_postgres_store),
    publish_store: PublishStore = Depends(get_publish_store),
    podcast_store: PodcastStore = Depends(get_podcast_store),
    activitypub_store: ActivityPubStore = Depends(get_activitypub_store),
    activitypub_delivery_client: ActivityPubDeliveryClient = Depends(
        get_activitypub_delivery_client
    ),
    broker_client: BrokerClient = Depends(get_broker_client),
    finalization_worker: Any = Depends(get_live_finalization_worker),
    caption_job_store: OfflineCaptionJobStore | None = Depends(get_caption_job_store),
    caption_settings: RecordedCaptionSettings = Depends(get_recorded_caption_settings),
    provider_registry: ProviderRegistry = Depends(get_provider_registry),
    subscribe_store: SubscribeStore = Depends(get_subscribe_store),
    delivery_store: NotificationDeliveryStore | None = Depends(get_notification_delivery_store),
    target_lookup: ChannelAssociationLookup | None = Depends(get_publication_target_lookup),
) -> PublishAssetStatus:
    """Approve per-surface publication through the configured providers."""
    asset_store = _require_asset_store(postgres_store)
    asset = asset_store.get_staff_row(asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )
    staff_asset = cast(StaffAssetRow, asset)
    if not staff_asset.manifest_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Publish preflight blocked: this asset has no manifest_url. "
                "Run the packager or fix ingest before approving publish."
            ),
        )
    # Two gates stand in front of approval, and both must pass before any
    # surface publishes. They are ordered caption-first deliberately.
    #
    # WP-02: publication is what starts the caption obligation, and the public
    # record must not wait days for caption review (so publish-first stays) --
    # but a station that cannot even record the obligation must not publish at
    # all. Queueing here means there is no window in which a recording is
    # public with no caption job: either both happen or neither does, and the
    # operator gets a 409 naming the cause instead of a green publish and a
    # line in a log file.
    try:
        _queue_offline_captions(
            caption_job_store,
            staff_asset,
            finalization_worker=finalization_worker,
            caption_settings=caption_settings,
            require_published=False,
        )
    except CaptionJobNotQueueableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=CAPTION_JOB_UNQUEUEABLE_DETAIL.format(cause=str(exc)),
        ) from exc
    try:
        record = approve_publish(
            asset=staff_asset,
            request=request,
            store=publish_store,
            media_path=_resolve_publish_media(finalization_worker, staff_asset),
            podcast_store=podcast_store,
            registry=provider_registry,
            subscribe_store=subscribe_store,
            delivery_store=delivery_store,
            target_lookup=target_lookup,
        )
    except PublishConfigurationError as exc:
        # WP-03: a selected surface's real-provider config is missing/invalid
        # -- a controlled 409 before any side effect, never an uncaught 500.
        #
        # Interaction with the caption gate above, stated because WP-03 makes
        # this path routine rather than rare: reaching here means the caption
        # job WAS queued and then nothing published. That is deliberate and
        # self-correcting, not a leak. enqueue_offline_caption_job is
        # idempotent per asset, so when the operator fixes the provider
        # configuration and approves again the same job is reused -- any
        # transcription it already did is reused too, not repeated. The
        # alternative ordering (validate providers, then queue captions) would
        # need this route to re-run approve_publish's own readiness gate
        # itself, and a gate evaluated in two places is a gate that drifts.
        # One gate, one owner; a queued job for an unpublished asset costs a
        # review-queue row that the retry consumes.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    staff_asset, record = _apply_portal_visibility(
        asset_store=asset_store,
        publish_store=publish_store,
        asset_id=asset_id,
        staff_asset=staff_asset,
        record=record,
    )
    record = send_pending_notifications_after_publication(
        asset=staff_asset,
        record=record,
        store=publish_store,
        subscribe_store=subscribe_store,
        delivery_store=delivery_store,
        registry=provider_registry,
        target_lookup=target_lookup,
    )
    status_response = build_publish_asset_status(staff_asset, record)
    broker_client.publish(build_publish_approved_event(status_response))
    activitypub_config = http_request.app.state.activitypub_config
    if activitypub_config.federation_mode == "disabled":
        return status_response
    activity = record_publish_activity(
        base_url=activitypub_config.base_url or str(http_request.base_url).rstrip("/"),
        status=status_response,
        store=activitypub_store,
    )
    if activity is not None:
        with suppress(ActivityPubRemoteError):
            deliver_publish_activity(
                record=activity,
                store=activitypub_store,
                delivery_client=activitypub_delivery_client,
            )
    return status_response


@staff_router.post(
    "/assets/{asset_id}/surfaces/{surface_id}/retry",
    response_model=PublishAssetStatus,
    summary="Retry one v0.7 publish surface",
    dependencies=[Depends(require_any_role("publish_operator"))],
    responses={
        404: {"description": "Asset or surface not found"},
        409: {"description": "Publish preflight blocked"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def retry_publish_asset_surface(
    asset_id: str,
    surface_id: str,
    request: PublishRetryRequest,
    postgres_store: Any = Depends(get_postgres_store),
    publish_store: PublishStore = Depends(get_publish_store),
    podcast_store: PodcastStore = Depends(get_podcast_store),
    caption_job_store: OfflineCaptionJobStore | None = Depends(get_caption_job_store),
    caption_settings: RecordedCaptionSettings = Depends(get_recorded_caption_settings),
    provider_registry: ProviderRegistry = Depends(get_provider_registry),
    subscribe_store: SubscribeStore = Depends(get_subscribe_store),
    delivery_store: NotificationDeliveryStore | None = Depends(get_notification_delivery_store),
    target_lookup: ChannelAssociationLookup | None = Depends(get_publication_target_lookup),
    # Same seam the approve route uses: a portal retry that makes a
    # LIVE-finalized recording public has to resolve its package directory
    # from the finalization job, not from the upload convention.
    finalization_worker: Any = Depends(get_live_finalization_worker),
) -> PublishAssetStatus:
    """Retry a single surface without changing the rest of the publish run."""
    asset_store = _require_asset_store(postgres_store)
    asset = asset_store.get_staff_row(asset_id)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )
    staff_asset = cast(StaffAssetRow, asset)
    if not staff_asset.manifest_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Publish retry blocked: this asset has no manifest_url. "
                "Run the packager or fix ingest before retrying publish."
            ),
        )
    try:
        record = retry_publish_surface(
            asset=staff_asset,
            surface_id=surface_id,
            request=request,
            store=publish_store,
            media_path=_resolve_publish_media(finalization_worker, staff_asset),
            podcast_store=podcast_store,
            registry=provider_registry,
            subscribe_store=subscribe_store,
            delivery_store=delivery_store,
            target_lookup=target_lookup,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except PublishConfigurationError as exc:
        # WP-03: the retried surface's real-provider config is missing/
        # invalid -- a controlled 409, same as the approve route, instead of
        # silently "retrying" a surface that was never going to succeed.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    staff_asset, record = _apply_portal_visibility(
        asset_store=asset_store,
        publish_store=publish_store,
        asset_id=asset_id,
        staff_asset=staff_asset,
        record=record,
    )
    record = send_pending_notifications_after_publication(
        asset=staff_asset,
        record=record,
        store=publish_store,
        subscribe_store=subscribe_store,
        delivery_store=delivery_store,
        registry=provider_registry,
        target_lookup=target_lookup,
    )
    # Only a Portal retry can be what makes the recording public when the
    # first approval failed, so only a Portal retry starts the caption
    # obligation. Any other surface (YouTube, Internet Archive, ...) retries
    # independently of captioning -- queueing here unconditionally would
    # re-transcribe an already-captioned asset every time an operator
    # retries an unrelated surface (audit finding 5).
    #
    # ``surface_id == "portal"`` alone is not enough: it only says which
    # surface was retried, not whether the retry actually made the asset
    # public. ``_apply_portal_visibility``'s docstring guarantees the
    # invariant this refinement leans on -- "Portal is not reported as
    # successful unless the canonical asset row actually becomes public" --
    # so a portal surface that is still not "succeeded" in the *returned*
    # record after that call (e.g. the retry's own ``asset_store
    # .mark_published`` write failed, same as the first-approval failure
    # path) means this retry did not publish anything, and must not start a
    # fresh transcription pass over a still-private recording.
    portal_became_public = any(
        surface.id == "portal" and surface.state == "succeeded" for surface in record.surfaces
    )
    if surface_id == "portal" and portal_became_public:
        # This retry is what made the recording public, so it carries the
        # same obligation as a first approval. It cannot be pre-checked the
        # way approval is -- the recording is already public by the time we
        # know the retry succeeded -- so the failure is surfaced rather than
        # swallowed: a public recording with no caption job and nothing but a
        # log line is the exact silence this route is not allowed to produce.
        # The message says plainly that the portal publish DID succeed, so
        # the operator does not read the 409 as "nothing happened", and
        # retrying the portal surface again after the fix is safe because the
        # enqueue is idempotent per asset.
        try:
            _queue_offline_captions(
                caption_job_store,
                staff_asset,
                finalization_worker=finalization_worker,
                caption_settings=caption_settings,
            )
        except CaptionJobNotQueueableError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The portal retry succeeded and this recording is now public, but "
                    "CivicCast could not queue its caption job, so no captions are on "
                    f"the way. Cause: {exc} Fix that and retry the portal surface again "
                    "to start captioning."
                ),
            ) from exc
    return build_publish_asset_status(staff_asset, record)
