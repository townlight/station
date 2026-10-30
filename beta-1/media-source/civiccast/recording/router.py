# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S21 scheduled-recording API surface (slice 3).

One staff family over the ``RecordingStore`` + ``RecordingService`` DI
seams:

* **Staff CRUD** (``/api/staff/recording/schedules[/{id}]``,
  ``/schedules/{id}/record-now``, ``/jobs``, ``/jobs/{id}/stop``) —
  the spec §4 roles are ``setup_admin`` + ``meeting_operator`` for
  write; ``support_admin`` is added on read so support staff can audit
  the job table without being able to mutate it.

DI seams (``get_recording_store`` / ``get_recording_service``) return
``None`` at import so the module opens no database; the app factory
overrides them in ``_wire_durable_stores`` and wires the scheduled
capture/finalize/alert runtime. A missing service is a 503, never a
silent 200 against storage that is not there.
``x-required-roles`` is mirrored into the generated OpenAPI so the
published contract cannot drift from the runtime role gate (same
convention as S23 / S24 / S25 / S26).
"""

from __future__ import annotations

import functools
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from civiccast.auth.roles import require_any_role
from civiccast.recording.input_presets import RecordingInputPreset, RecordingInputPresetCatalog
from civiccast.recording.models import (
    JobState,
    RecordingJob,
    RecordingSchedule,
    RecordingScheduleInput,
    RecordingScheduleUpdate,
)
from civiccast.recording.service import (
    RecordingPipelineFailureError,
    RecordingPipelineUnwiredError,
    RecordingService,
)
from civiccast.recording.store import (
    RecordingJobIdConflictError,
    RecordingJobNotFoundError,
    RecordingJobStateError,
    RecordingScheduleNameConflictError,
    RecordingScheduleNotFoundError,
    RecordingStore,
)

logger = logging.getLogger(__name__)

_DB_NOT_READY = "Durable storage is not ready yet."

# Spec §4 — write requires the schedule/operate roles; read also admits
# the support role so an on-call support_admin can audit the job table
# without being able to mutate a schedule.
_WRITE = ("setup_admin", "meeting_operator")
_READ = ("setup_admin", "meeting_operator", "support_admin")
_WRITE_EXTRA = {"x-required-roles": list(_WRITE)}
_READ_EXTRA = {"x-required-roles": list(_READ)}

#: Cap on ``GET /jobs?limit=``. 200 default, 1000 hard cap; anything
#: above the hard cap is clamped (not rejected) so an operator UI that
#: forgot to pass a limit still gets a sensible page.
_DEFAULT_JOB_LIMIT = 200
_HARD_JOB_LIMIT = 1_000

_DEFAULT_STATION_ID = "civiccast-station"


@functools.cache
def _station_id() -> str:
    """The active station id (single-station deployment; env-overridable).

    Cached because the station id is process-fixed in the deployed
    posture — re-reading ``os.environ`` on every read was a hot-path
    nit (Q-5 on the agenda audit). Tests that need a different station
    id should set the env BEFORE importing this module or call
    ``_station_id.cache_clear()`` in a fixture.
    """
    return os.environ.get("CIVICCAST_STATION_ID") or _DEFAULT_STATION_ID


# --- DI seams (overridden by the app factory) --------------------------------


def get_recording_store() -> RecordingStore | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def get_recording_service() -> RecordingService | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def get_recording_input_catalog() -> RecordingInputPresetCatalog | None:
    """DI seam for locally detected and station-configured capture inputs."""
    return None


def _require_store(store: RecordingStore | None) -> RecordingStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_service(svc: RecordingService | None) -> RecordingService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


# --- routers -----------------------------------------------------------------


staff_router = APIRouter(prefix="/api/staff", tags=["staff", "recording"])


@staff_router.get(
    "/recording/input-presets",
    response_model=list[RecordingInputPreset],
    summary="List detected and configured SDI/HDMI recording inputs",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_input_presets(
    refresh: bool = Query(False, description="Re-run local FFmpeg device discovery."),
    catalog: RecordingInputPresetCatalog | None = Depends(get_recording_input_catalog),
) -> list[RecordingInputPreset]:
    if catalog is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return catalog.list_presets(refresh=refresh)


# --- schedules ---------------------------------------------------------------


@staff_router.get(
    "/recording/schedules",
    response_model=list[RecordingSchedule],
    summary="List recording schedules for the station",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_schedules(
    enabled_only: bool = Query(
        False,
        description="Filter to schedules with ``enabled=True`` only.",
    ),
    store: RecordingStore | None = Depends(get_recording_store),
) -> list[RecordingSchedule]:
    """All schedules for the active station."""
    return _require_store(store).list_schedules(_station_id(), enabled_only=enabled_only)


@staff_router.post(
    "/recording/schedules",
    response_model=RecordingSchedule,
    status_code=status.HTTP_201_CREATED,
    summary="Create a recording schedule",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_EXTRA,
    responses={
        403: {"description": "Payload station_id does not match the deployed station."},
        409: {
            "description": (
                "A schedule with this id already exists, or another schedule already "
                "uses (station_id, name)."
            )
        },
        503: {"description": _DB_NOT_READY},
    },
)
def create_schedule(
    payload: RecordingScheduleInput,
    store: RecordingStore | None = Depends(get_recording_store),
) -> RecordingSchedule:
    """Create one schedule. POST against an existing ``schedule_id`` is a
    409 — use PATCH to update.

    Q-5 fix: pre-fix ``payload.station_id`` was operator-supplied and
    persisted verbatim; in a single-station deployment that created a
    write-only orphan invisible to the deployed station's reads. We now
    require the payload's ``station_id`` to match the deployment's
    configured station — anything else is a 403. When multi-tenancy
    lands (the stated CivicCast roadmap pattern), the deployment will
    move ``station_id`` resolution into a per-request dependency rather
    than the env-fixed singleton; the same gate continues to apply.
    """
    resolved = _require_store(store)
    deployment_station = _station_id()
    if payload.station_id != deployment_station:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=(
                f"Schedule station_id {payload.station_id!r} does not match "
                f"the deployed station ({deployment_station!r})."
            ),
        )
    if resolved.get_schedule(payload.schedule_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Recording schedule {payload.schedule_id!r} already exists. Use PATCH to update.",
        )
    try:
        return resolved.upsert_schedule(RecordingSchedule(**payload.model_dump()))
    except RecordingScheduleNameConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/recording/schedules/{schedule_id}",
    response_model=RecordingSchedule,
    summary="Get one recording schedule",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_EXTRA,
    responses={
        404: {"description": "Recording schedule not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def get_schedule(
    schedule_id: str,
    store: RecordingStore | None = Depends(get_recording_store),
) -> RecordingSchedule:
    schedule = _require_store(store).get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Recording schedule {schedule_id!r} not found.",
        )
    return schedule


@staff_router.patch(
    "/recording/schedules/{schedule_id}",
    response_model=RecordingSchedule,
    summary="Patch a recording schedule (absent keys unchanged)",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_EXTRA,
    responses={
        404: {"description": "Recording schedule not found"},
        409: {"description": "Patch would conflict with another schedule's (station_id, name)"},
        503: {"description": _DB_NOT_READY},
    },
)
def patch_schedule(
    schedule_id: str,
    payload: RecordingScheduleUpdate,
    store: RecordingStore | None = Depends(get_recording_store),
    svc: RecordingService | None = Depends(get_recording_service),
) -> RecordingSchedule:
    """Patch semantics: absent key unchanged. ``schedule_id`` /
    ``station_id`` are set at creation and not editable here.

    Patch semantics (E-10 fix):

    * Omitted keys are left at their current value.
    * Sending an explicit ``null`` clears a nullable field (e.g.
      ``"target_series": null`` removes the target series).
    * ``custom_field_values`` accepts either ``null`` (clears) or a
      dict (replaces — the patch is whole-blob, NOT key-merge).

    Side effect (E-11 fix): flipping ``enabled`` from ``True`` to
    ``False`` ALSO cancels every still-``scheduled`` job for this
    schedule (transitions them to ``skipped`` with reason
    ``"schedule disabled"``). Pre-fix the disable was a no-op for
    materialized jobs, so an operator who disabled a schedule mid-window
    expected the next firing not to happen and got the opposite.
    """
    resolved = _require_store(store)
    current = resolved.get_schedule(schedule_id)
    if current is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Recording schedule {schedule_id!r} not found.",
        )
    # Keep validated nested models: model_dump() turns recurrence into a dict
    # whose datetime bypasses the store's model-to-JSON serialization path.
    updates = {key: getattr(payload, key) for key in payload.model_fields_set}
    merged = current.model_copy(update=updates)
    try:
        result = resolved.upsert_schedule(merged)
    except RecordingScheduleNameConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    # E-11: cancel future jobs when the patch disables the schedule.
    if current.enabled and merged.enabled is False and svc is not None:
        try:
            svc.cancel_scheduled_jobs_for_schedule(schedule_id)
        except Exception:
            # Best-effort cancellation; the schedule edit itself is
            # already durable. A future tick's overlap check will not
            # mis-arm a job we didn't manage to cancel here because the
            # schedule is now disabled and the materializer's
            # ``enabled_only=True`` filter keeps it out.
            logger.exception(
                "recording.patch_schedule failed to cancel future jobs for schedule_id=%s",
                schedule_id,
            )
    return result


@staff_router.delete(
    "/recording/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a recording schedule",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_EXTRA,
    responses={
        404: {"description": "Recording schedule not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_schedule(
    schedule_id: str,
    store: RecordingStore | None = Depends(get_recording_store),
) -> Response:
    """Delete a schedule. Existing jobs that reference the schedule are
    left in place — the operator may want to see the job history even
    after the recurring schedule is retired."""
    try:
        _require_store(store).delete_schedule(schedule_id)
    except RecordingScheduleNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@staff_router.post(
    "/recording/schedules/{schedule_id}/record-now",
    response_model=RecordingJob,
    summary="Start an ad-hoc recording against this schedule's source NOW",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_EXTRA,
    responses={
        404: {"description": "Recording schedule not found"},
        409: {"description": "An overlapping recording is already armed/active on this source"},
        500: {"description": "Capture pipeline failed during arm/start"},
        503: {"description": "Scheduled recording runtime is unavailable in this deployment"},
    },
)
def record_now(
    schedule_id: str,
    svc: RecordingService | None = Depends(get_recording_service),
) -> RecordingJob:
    """Ad-hoc capture against the schedule's source.

    Runs through the same state machine as a scheduled job
    (``arm → start``) so the operator UI sees a consistent shape;
    finalize lands later (operator stop, or the scheduler's window-end
    tick for jobs created with a duration).

    503 only when a deployment override leaves the runtime unavailable;
    the production app factory wires capture/finalize/alert adapters.
    409 when an overlap is detected at arm time."""
    resolved = _require_service(svc)
    try:
        return resolved.record_now(schedule_id)
    except RecordingScheduleNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RecordingJobIdConflictError as exc:
        # E-6 fix: a job_id collision (real Slug regex mismatch on the
        # schedule_id post E-1) is a 409, not a 500.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RecordingPipelineUnwiredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except RecordingPipelineFailureError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


# --- jobs --------------------------------------------------------------------


@staff_router.get(
    "/recording/jobs",
    response_model=list[RecordingJob],
    summary="List recording jobs for the station",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_jobs(
    state: JobState | None = Query(
        None,
        description="Optional state filter (e.g. ``scheduled`` for the upcoming queue).",
    ),
    schedule_id: str | None = Query(
        None,
        description="Optional schedule filter — the job history for one recurring schedule.",
    ),
    limit: int = Query(
        _DEFAULT_JOB_LIMIT,
        ge=1,
        description=(
            "Max rows to return. Default 200; values above the hard cap "
            f"({_HARD_JOB_LIMIT}) are clamped."
        ),
    ),
    store: RecordingStore | None = Depends(get_recording_store),
) -> list[RecordingJob]:
    """List jobs (newest planned first).

    ``limit`` is clamped at :data:`_HARD_JOB_LIMIT` so a forgetful
    operator UI cannot exhaust process memory with a 1M-row read."""
    effective_limit = min(limit, _HARD_JOB_LIMIT)
    return _require_store(store).list_jobs(
        _station_id(),
        state=state,
        schedule_id=schedule_id,
        limit=effective_limit,
    )


@staff_router.post(
    "/recording/jobs/{job_id}/stop",
    response_model=RecordingJob,
    summary="Stop a running recording job",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_EXTRA,
    responses={
        404: {"description": "Recording job not found"},
        409: {"description": "Job is not in an active state (already done / failed / skipped)"},
        503: {"description": "Scheduled recording runtime is unavailable in this deployment"},
    },
)
def stop_job(
    job_id: str,
    svc: RecordingService | None = Depends(get_recording_service),
) -> RecordingJob:
    """Operator-initiated stop. Finalizes whatever partial bytes the
    capture pipeline has written into a normal asset (DC-4)."""
    resolved = _require_service(svc)
    try:
        return resolved.stop_job(job_id)
    except RecordingJobNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RecordingJobStateError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RecordingPipelineUnwiredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except RecordingPipelineFailureError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


__all__ = [
    "get_recording_service",
    "get_recording_store",
    "staff_router",
]
