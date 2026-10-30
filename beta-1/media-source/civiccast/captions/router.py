# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for the caption review queue."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from civiccast.auth.roles import require_any_role
from civiccast.captions.external import (
    ExternalCaptionIngestRequest,
    ExternalCaptionIngestResult,
    ExternalCaptionParseError,
    ingest_external_caption_review_items,
)
from civiccast.captions.review import (
    CaptionReviewAudioEvidenceRequiredError,
    CaptionReviewDecision,
    CaptionReviewEdit,
    CaptionReviewItemAlreadyExistsError,
    CaptionReviewItemCreate,
    CaptionReviewItemNotFoundError,
    CaptionReviewItemRequest,
    CaptionReviewItemResponse,
    CaptionReviewLowConfidenceAcknowledgementRequiredError,
    CaptionReviewStatus,
    CaptionReviewStore,
)
from civiccast.captions.review_media import (
    CaptionReviewClipBuilder,
    CaptionReviewClipError,
    build_caption_review_clip,
    cue_relative_to_audio_evidence,
    resolve_caption_review_source,
    verify_caption_review_audio_evidence_for_cue,
)
from civiccast.captions.settings import RecordedCaptionSettings
from civiccast.captions.settings_router import get_recorded_caption_settings
from civiccast.captions.vod_job import (
    OFFLINE_CAPTION_JOB_STATE_FAILED,
    OFFLINE_CAPTION_JOB_STATE_PENDING,
    OfflineCaptionJobConflictError,
    OfflineCaptionJobRecord,
    OfflineCaptionJobState,
    OfflineCaptionJobStore,
)
from civiccast.live.router import get_live_finalization_worker
from civiccast.platform.stores import resolve_app_store
from civiccast.stream._ffmpeg import FfmpegNotFoundError

staff_router = APIRouter(prefix="/api/staff/captions", tags=["staff", "captions"])

_CAPTION_JOB_STORE_NOT_READY_DETAIL = (
    "Offline caption job store is not configured for this app instance. "
    "Restart CivicCast through create_app() or configure the store bundle."
)


def get_caption_review_store(request: Request) -> CaptionReviewStore:
    """FastAPI dependency for the active caption review queue store."""

    return cast(
        CaptionReviewStore,
        resolve_app_store(request, "caption_review_store", surface="Caption review store"),
    )


def get_caption_review_asset_store(request: Request) -> Any:
    """FastAPI dependency for resolving a review item's local asset media."""

    return resolve_app_store(request, "asset_store", surface="Caption review asset store")


def get_caption_review_clip_builder() -> CaptionReviewClipBuilder:
    """Dependency seam for the bounded ffmpeg review-clip builder."""

    return build_caption_review_clip


def get_offline_caption_job_store(request: Request) -> OfflineCaptionJobStore | None:
    """FastAPI dependency for the offline caption job queue (K3), when configured.

    Optional like ``get_caption_job_store`` in ``publish.router``: a station
    running without durable storage has nowhere to keep a job that spans an
    operator's review, so these endpoints report 503 rather than pretending.
    """

    return cast(
        "OfflineCaptionJobStore | None",
        resolve_app_store(request, "caption_job_store", surface="Offline caption job store"),
    )


@staff_router.post(
    "/review-items",
    response_model=CaptionReviewItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a stable caption cue to the review queue",
    dependencies=[Depends(require_any_role("records_clerk", "support_admin"))],
    responses={
        409: {"description": "review_item_id already exists"},
        422: {"description": "Invalid payload"},
    },
)
def create_review_item(
    payload: CaptionReviewItemRequest,
    store: CaptionReviewStore = Depends(get_caption_review_store),
) -> CaptionReviewItemResponse:
    try:
        return store.create(CaptionReviewItemCreate.model_validate(payload.model_dump()))
    except CaptionReviewItemAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Caption review item already exists: {exc.review_item_id}",
        ) from exc


@staff_router.post(
    "/external-ingest",
    response_model=ExternalCaptionIngestResult,
    status_code=status.HTTP_201_CREATED,
    summary="Add external caption appliance output to the review queue",
    dependencies=[Depends(require_any_role("records_clerk", "support_admin"))],
    responses={
        400: {"description": "External caption payload did not contain reviewable timed cues"},
        409: {"description": "review_item_id already exists"},
        422: {"description": "Invalid payload"},
    },
)
def create_external_caption_review_items(
    payload: ExternalCaptionIngestRequest,
    store: CaptionReviewStore = Depends(get_caption_review_store),
) -> ExternalCaptionIngestResult:
    try:
        return ingest_external_caption_review_items(payload, store)
    except ExternalCaptionParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except CaptionReviewItemAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Caption review item already exists: {exc.review_item_id}",
        ) from exc


@staff_router.get(
    "/review-items",
    response_model=list[CaptionReviewItemResponse],
    summary="List caption review queue items",
)
def list_review_items(
    asset_id: str | None = None,
    status_filter: CaptionReviewStatus | None = None,
    language: str | None = None,
    store: CaptionReviewStore = Depends(get_caption_review_store),
) -> list[CaptionReviewItemResponse]:
    """List review-queue rows.

    ``language`` scopes to one review pass -- ``en`` (English transcription)
    or ``es`` (recorded-Spanish translation). Omitted, both are returned so
    the operator console can render the language badge and filter
    client-side; the parameter lets a caller fetch just one queue directly.
    """

    return store.list(asset_id=asset_id, status=status_filter, language=language)


@staff_router.get(
    "/review-items/{review_item_id}",
    response_model=CaptionReviewItemResponse,
    summary="Get one caption review queue item",
    responses={404: {"description": "Caption review item not found"}},
)
def get_review_item(
    review_item_id: str,
    store: CaptionReviewStore = Depends(get_caption_review_store),
) -> CaptionReviewItemResponse:
    item = store.get(review_item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caption review item not found: {review_item_id}",
        )
    return item


@staff_router.get(
    "/review-items/{review_item_id}/clip",
    response_class=FileResponse,
    summary="Play bounded local audio around one caption cue",
    responses={
        200: {
            "description": (
                "Inline WAV audio for authenticated review; clients must not "
                "store the private clip (Cache-Control: private, no-store)."
            ),
            "content": {
                "audio/wav": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                    }
                }
            },
        },
        404: {"description": "Caption review item or asset not found"},
        409: {"description": "Asset has no readable local media"},
        422: {"description": "Asset audio could not be decoded"},
        503: {"description": "Caption review audio tooling is unavailable"},
    },
)
def get_review_item_clip(
    review_item_id: str,
    store: CaptionReviewStore = Depends(get_caption_review_store),
    asset_store: Any = Depends(get_caption_review_asset_store),
    clip_builder: CaptionReviewClipBuilder = Depends(get_caption_review_clip_builder),
) -> FileResponse:
    """Return a short authenticated WAV clip and remove it after transfer."""

    item = store.get(review_item_id)
    if item is None:
        raise _not_found(review_item_id)

    cue = item.cue
    evidence = store.get_audio_evidence(review_item_id)
    if evidence is not None:
        try:
            source = verify_caption_review_audio_evidence_for_cue(
                evidence,
                item.cue,
            )
            cue = cue_relative_to_audio_evidence(item.cue, evidence)
        except CaptionReviewClipError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
    else:
        get_staff_row = getattr(asset_store, "get_staff_row", None)
        asset = get_staff_row(item.asset_id) if callable(get_staff_row) else None
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Caption review asset not found: {item.asset_id}",
            )
        stored_path = getattr(asset, "file_path", None)
        resolved_source = resolve_caption_review_source(stored_path) if stored_path else None
        if resolved_source is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This caption cue has no local meeting media to review.",
            )
        source = resolved_source.expanduser().resolve()
        if not source.is_file():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The local meeting media is missing. Relink or restore the asset, then retry."
                ),
            )

    try:
        clip = clip_builder(source, cue)
    except FfmpegNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Caption review audio is unavailable because ffmpeg is not installed.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Caption review audio timed out. Retry after checking system load.",
        ) from exc
    except CaptionReviewClipError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    if not clip.is_file():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Caption review audio was not produced. Retry or check caption service logs.",
        )
    return FileResponse(
        path=clip,
        media_type="audio/wav",
        filename=f"{review_item_id}-caption-review.wav",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
        background=BackgroundTask(Path.unlink, clip, missing_ok=True),
    )


@staff_router.post(
    "/review-items/{review_item_id}/approve",
    response_model=CaptionReviewItemResponse,
    summary="Approve a caption review item",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={
        404: {"description": "Caption review item not found"},
        409: {"description": "Low-confidence cue was not explicitly acknowledged"},
    },
)
def approve_review_item(
    review_item_id: str,
    payload: CaptionReviewDecision,
    store: CaptionReviewStore = Depends(get_caption_review_store),
) -> CaptionReviewItemResponse:
    try:
        return store.approve(review_item_id, payload)
    except CaptionReviewItemNotFoundError as exc:
        raise _not_found(exc.review_item_id) from exc
    except CaptionReviewLowConfidenceAcknowledgementRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This is a low-confidence caption cue. Compare it with the retained "
                "audio, then explicitly acknowledge that review before approval."
            ),
        ) from exc
    except CaptionReviewAudioEvidenceRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This low-confidence caption cannot be approved because its "
                f"retained audio evidence is unavailable or invalid: {exc.reason}"
            ),
        ) from exc


@staff_router.post(
    "/review-items/{review_item_id}/edit",
    response_model=CaptionReviewItemResponse,
    summary="Edit a caption review item",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={404: {"description": "Caption review item not found"}},
)
def edit_review_item(
    review_item_id: str,
    payload: CaptionReviewEdit,
    store: CaptionReviewStore = Depends(get_caption_review_store),
) -> CaptionReviewItemResponse:
    try:
        return store.edit(review_item_id, payload)
    except CaptionReviewAudioEvidenceRequiredError as exc:
        raise HTTPException(
            409, f"Timing edit must stay within retained review audio: {exc.reason}"
        ) from exc
    except CaptionReviewItemNotFoundError as exc:
        raise _not_found(exc.review_item_id) from exc


@staff_router.post(
    "/review-items/{review_item_id}/reject",
    response_model=CaptionReviewItemResponse,
    summary="Reject a caption review item",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={404: {"description": "Caption review item not found"}},
)
def reject_review_item(
    review_item_id: str,
    payload: CaptionReviewDecision,
    store: CaptionReviewStore = Depends(get_caption_review_store),
) -> CaptionReviewItemResponse:
    try:
        return store.reject(review_item_id, payload)
    except CaptionReviewItemNotFoundError as exc:
        raise _not_found(exc.review_item_id) from exc


@staff_router.get(
    "/offline-jobs",
    response_model=list[OfflineCaptionJobRecord],
    summary="List offline caption jobs (K3) for operator visibility",
    responses={503: {"description": "Offline caption job store is not configured"}},
)
def list_offline_caption_jobs(
    asset_id: str | None = None,
    state: OfflineCaptionJobState | None = None,
    store: OfflineCaptionJobStore | None = Depends(get_offline_caption_job_store),
) -> list[OfflineCaptionJobRecord]:
    """Return offline caption job rows -- state, attempts, last_error.

    Audit finding 4: state/attempts/last_error were persisted by the K3
    worker but nothing surfaced them; the only "retry" was re-approving
    publish. This is the read side of that gap.

    Wired to the operator console's per-asset captions drawer
    (``OfflineCaptionJobsPanel``, ``civiccast/apps/portal-operator/src/
    screens/OfflineCaptionJobsPanel.tsx``), which calls this scoped to the
    open asset and offers a Retry action on failed rows.
    """
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_CAPTION_JOB_STORE_NOT_READY_DETAIL,
        )
    return store.list(asset_id=asset_id, state=state)


class CaptionBatchRequest(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=10)


class CaptionBatchResult(BaseModel):
    asset_id: str
    job: OfflineCaptionJobRecord | None = None
    error: str | None = None


@staff_router.post(
    "/offline-jobs/batch",
    response_model=list[CaptionBatchResult],
    dependencies=[Depends(require_any_role("records_clerk"))],
    summary="Queue up to ten selected recordings for caption review",
)
def queue_caption_batch(
    payload: CaptionBatchRequest,
    store: OfflineCaptionJobStore | None = Depends(get_offline_caption_job_store),
    assets: Any = Depends(get_caption_review_asset_store),
    finalization_worker: Any = Depends(get_live_finalization_worker),
    settings: RecordedCaptionSettings = Depends(get_recorded_caption_settings),
    reviews: CaptionReviewStore = Depends(get_caption_review_store),
) -> list[CaptionBatchResult]:
    from civiccast.publish.router import (
        CaptionJobNotQueueableError,
        _queue_offline_captions,
        _resolve_caption_package_dir,
    )

    if store is None:
        raise HTTPException(503, _CAPTION_JOB_STORE_NOT_READY_DETAIL)
    if not settings.enabled:
        raise HTTPException(409, "Enable recorded captions in Settings before starting a batch.")
    results = []
    for asset_id in dict.fromkeys(payload.asset_ids):
        asset = assets.get_staff_row(asset_id)
        error = None
        if asset is None:
            error = "Recording no longer exists."
        elif not asset.file_path or not Path(asset.file_path).is_file():
            error = "A local recording file is required."
        else:
            package = _resolve_caption_package_dir(asset_id, finalization_worker)
            if package is None or not (package / "playlist.m3u8").is_file():
                error = "Package this recording in Assets before captioning it."
        if error:
            results.append(CaptionBatchResult(asset_id=asset_id, error=error))
            continue
        try:
            previous = store.list(asset_id=asset_id)
            if (
                store.active_for_asset(asset_id) is None
                and previous
                and previous[-1].cue_count > 0
                and reviews.list(asset_id=asset_id)
            ):
                latest = previous[-1]
                job = store.save(
                    latest.model_copy(
                        update={
                            "state": "awaiting_review",
                            "attempts": 0,
                            "last_error": "",
                            "next_attempt_at": datetime.now(UTC),
                            "updated_at": datetime.now(UTC),
                        }
                    )
                )
                results.append(CaptionBatchResult(asset_id=asset_id, job=job))
                continue
            _queue_offline_captions(
                store,
                asset,
                finalization_worker=finalization_worker,
                caption_settings=settings,
                require_published=False,
            )
            results.append(
                CaptionBatchResult(asset_id=asset_id, job=store.active_for_asset(asset_id))
            )
        except (CaptionJobNotQueueableError, OfflineCaptionJobConflictError) as exc:
            results.append(CaptionBatchResult(asset_id=asset_id, error=str(exc)))
    return results


@staff_router.post(
    "/offline-jobs/{job_id}/cancel",
    response_model=OfflineCaptionJobRecord,
    dependencies=[Depends(require_any_role("records_clerk"))],
    summary="Cancel remaining caption work at the next audio chunk boundary",
)
def cancel_offline_caption_job(
    job_id: str,
    store: OfflineCaptionJobStore | None = Depends(get_offline_caption_job_store),
) -> OfflineCaptionJobRecord:
    if store is None:
        raise HTTPException(503, _CAPTION_JOB_STORE_NOT_READY_DETAIL)
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Caption job not found.")
    if job.state not in ("pending", "awaiting_review", "cancelled"):
        raise HTTPException(409, "Only queued or review-stage caption work can be cancelled.")
    return store.save(
        job.model_copy(
            update={
                "state": "cancelled",
                "next_attempt_at": None,
                "last_error": "Cancelled by an operator. Retained cues and review decisions are preserved.",
                "updated_at": datetime.now(UTC),
            }
        )
    )


@staff_router.post(
    "/offline-jobs/{job_id}/retry",
    response_model=OfflineCaptionJobRecord,
    summary="Retry failed or resume cancelled offline caption work",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={
        404: {"description": "Offline caption job not found"},
        409: {"description": "Only failed or cancelled jobs can be retried or resumed"},
        503: {"description": "Offline caption job store is not configured"},
    },
)
def retry_offline_caption_job(
    job_id: str,
    store: OfflineCaptionJobStore | None = Depends(get_offline_caption_job_store),
) -> OfflineCaptionJobRecord:
    """Retry failed or resume cancelled work without re-approving publication.

    The existing recovery path is re-approving publish for the asset, which
    re-transcribes from scratch and requires the ``publish_operator`` role
    (see docs/ops/background-workers.md). This gives an operator who works
    the caption queue -- and can see *why* a job died -- a direct way to
    give it a fresh attempt budget, gated on ``records_clerk`` like the rest
    of this router's mutations.

    Audit finding (MAJOR): this used to call ``store.save(...)`` directly,
    without checking ``active_for_asset`` first -- the same one-active-job-
    per-asset invariant ``enqueue_offline_caption_job`` guards. A FAILED job
    is not "active" (see ``OFFLINE_CAPTION_JOB_ACTIVE_STATES``), but the
    asset can still have picked up a *different* active job in the
    meantime (a republish, or a second operator's retry) between the failed
    job dying and this retry landing. Reopening the failed job unconditionally
    would put two active jobs on one asset: on the durable store that trips
    the DB-level partial-unique index
    (``ix_offline_caption_jobs_one_active_per_asset``, 0075) as an unhandled
    error; on the in-memory store it has no guard at all and silently
    doubles up. Defense in depth, two layers:

    1. Pre-check ``active_for_asset`` here and refuse with 409 when a
       *different* job already holds the asset's active slot.
    2. ``PostgresOfflineCaptionJobStore.save`` (persistence.py) now also
       catches the DB-level ``IntegrityError`` from the same index and
       raises ``OfflineCaptionJobConflictError``, closing the TOCTOU window
       between the check above and the write below; caught here too so a
       race never surfaces as a raw 500.
    """
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_CAPTION_JOB_STORE_NOT_READY_DETAIL,
        )
    job = store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Offline caption job not found: {job_id}",
        )
    if job.state not in (OFFLINE_CAPTION_JOB_STATE_FAILED, "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Offline caption job {job_id} is {job.state!r}; only a failed job "
                "or cancelled job can be manually retried."
            ),
        )
    conflict = store.active_for_asset(job.asset_id)
    if conflict is not None and conflict.job_id != job_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Offline caption job {conflict.job_id!r} is already active for asset "
                f"{job.asset_id!r}; retry {job_id} once it finishes."
            ),
        )
    now = datetime.now(UTC)
    try:
        return store.save(
            job.model_copy(
                update={
                    "state": "awaiting_review"
                    if job.state == "cancelled" and job.cue_count > 0
                    else OFFLINE_CAPTION_JOB_STATE_PENDING,
                    "last_error": "",
                    # A fresh attempt budget, same as a newly-enqueued job --
                    # otherwise one more failure would burn straight back to
                    # `failed` with no backoff, defeating the point of the
                    # manual retry.
                    "attempts": 0,
                    "next_attempt_at": now,
                    "updated_at": now,
                }
            )
        )
    except OfflineCaptionJobConflictError as exc:
        # The pre-check above lost the TOCTOU race: another caller's
        # enqueue/save won the asset's active slot between the check and
        # this write. Surface the same clean 409 rather than a raw 500.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Offline caption job {job_id} could not be retried: another job is now "
                f"active for asset {job.asset_id!r}."
            ),
        ) from exc


def _not_found(review_item_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Caption review item not found: {review_item_id}",
    )
