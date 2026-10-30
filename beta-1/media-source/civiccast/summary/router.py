# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for v0.6 sourced summary review."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from civiccast.agenda.router import get_agenda_service, get_agenda_store
from civiccast.agenda.service import AgendaService
from civiccast.agenda.store import AgendaStore
from civiccast.ai_runtime.ollama_client import OllamaRuntimeUnavailableError
from civiccast.auth.roles import require_any_role
from civiccast.captions import CaptionCue
from civiccast.captions.review import CaptionReviewStore
from civiccast.captions.router import get_caption_review_store
from civiccast.captions.vod import reviewed_caption_cues
from civiccast.platform.stores import resolve_app_store
from civiccast.recording.router import get_recording_store
from civiccast.summary.csv_export import render_transcript_csv
from civiccast.summary.fingerprint import sha256_fingerprint
from civiccast.summary.generate import SummaryGenerationPipeline, SummaryModel
from civiccast.summary.job import (
    SUMMARY_JOB_STATE_FAILED,
    SUMMARY_JOB_STATE_PENDING,
    SummaryGenerationJobConflictError,
    SummaryGenerationJobRecord,
    SummaryGenerationJobState,
    SummaryGenerationJobStore,
    enqueue_summary_job,
)
from civiccast.summary.minutes import (
    render_minutes,
    transcript_fingerprint,
    validate_minutes_sources,
)
from civiccast.summary.models import (
    AgendaMinutes,
    MinutesContent,
    MinutesSection,
    ModelProvenance,
    OperatorApproval,
    SummaryDraft,
)
from civiccast.summary.ollama import OllamaSummaryModel, SummaryResourceDeferredError
from civiccast.summary.store import (
    SummaryStore,
    SummaryStoreConflictError,
    SummaryStoreNotFoundError,
)

staff_router = APIRouter(prefix="/api/staff/summaries", tags=["staff", "summaries"])

_SUMMARY_JOB_STORE_NOT_READY_DETAIL = (
    "Summary generation job store is not configured for this app instance. "
    "Restart CivicCast through create_app() or configure the store bundle."
)

OLLAMA_NOT_CONFIGURED_MESSAGE = (
    "Local Ollama AI runtime is not reachable. Start Ollama and retry, or configure a "
    "different summary model in AI model settings."
)


def get_summary_store(request: Request) -> SummaryStore:
    """FastAPI dependency for the active summary review store."""

    return cast(
        SummaryStore, resolve_app_store(request, "summary_store", surface="Summary review store")
    )


def get_summary_job_store(request: Request) -> SummaryGenerationJobStore | None:
    """FastAPI dependency for the async summary generation job queue, when configured.

    Optional like ``get_offline_caption_job_store`` in ``captions.router``: a station
    running without durable storage has nowhere to keep a job that can legitimately
    span minutes of CPU-only generation (field evidence 2026-08-29 -- see
    civiccast/summary/job.py), so these endpoints report 503 rather than pretending.
    """

    return cast(
        "SummaryGenerationJobStore | None",
        resolve_app_store(request, "summary_job_store", surface="Summary generation job store"),
    )


def get_summary_model() -> SummaryModel:
    """FastAPI dependency for the active summary model (default: no durable storage).

    ``for_release()`` calls out to the local Ollama daemon to capture live model
    provenance; when Ollama isn't running/installed this raises
    ``OllamaRuntimeUnavailableError`` (unified in ``ai_runtime.ollama_client``
    for daemon-down, model-missing, and generate-call-failed alike). Report it
    the same clean "not configured" way the AI-models availability surface
    already does, rather than letting it bubble into a raw 500.

    When durable storage is wired, ``civiccast.app``'s app factory overrides
    this dependency with ``_resolve_summary_model`` (a separate path through
    ``ai_models.dispatch.build_summary_model`` to the same local-Ollama build)
    -- that override needs and has the identical try/except, since FastAPI
    calls whichever resolver is registered, not this one.
    """

    try:
        return OllamaSummaryModel.for_release()
    except OllamaRuntimeUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=OLLAMA_NOT_CONFIGURED_MESSAGE
        ) from exc


class SummaryReviewQueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SummaryDraft]
    next_cursor: str | None = None


class SummaryGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meeting_id: str
    cues: list[CaptionCue] = Field(default_factory=list)


class AgendaMinutesCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_summary_id: str | None = None


class AgendaMinutesEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_audit_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    content: MinutesContent


class SummaryApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_audit_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_note: str | None = None


class SummaryEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative: str = Field(min_length=1, max_length=12000)
    expected_audit_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


@staff_router.get(
    "/review-items",
    response_model=SummaryReviewQueueResponse,
    summary="List sourced summaries awaiting review or ready for export",
)
def list_review_items(
    store: SummaryStore = Depends(get_summary_store),
) -> SummaryReviewQueueResponse:
    return SummaryReviewQueueResponse(items=store.list_review_items(), next_cursor=None)


@staff_router.patch(
    "/{summary_id}",
    response_model=SummaryDraft,
    summary="Edit a pending summary narrative before approval",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={
        404: {"description": "Summary not found"},
        409: {"description": "Summary is no longer pending review or has changed"},
    },
)
def edit_summary(
    summary_id: str,
    payload: SummaryEditRequest,
    store: SummaryStore = Depends(get_summary_store),
) -> SummaryDraft:
    try:
        return store.edit_summary(
            summary_id,
            narrative=payload.narrative,
            expected_audit_fingerprint=payload.expected_audit_fingerprint,
        )
    except SummaryStoreNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Summary not found: {summary_id}",
        ) from exc
    except SummaryStoreConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.post(
    "/generate",
    response_model=SummaryDraft,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a sourced summary draft from committed transcript cues",
    dependencies=[Depends(require_any_role("records_clerk", "support_admin"))],
    responses={
        503: {
            "description": (
                "Local Ollama is unavailable or summary generation was deferred to preserve "
                "broadcast memory headroom."
            )
        }
    },
)
def generate_summary(
    payload: SummaryGenerateRequest,
    model: SummaryModel = Depends(get_summary_model),
    store: SummaryStore = Depends(get_summary_store),
) -> SummaryDraft:
    try:
        draft = SummaryGenerationPipeline(model=model).generate(
            meeting_id=payload.meeting_id,
            cues=payload.cues,
        )
    except SummaryResourceDeferredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except OllamaRuntimeUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=OLLAMA_NOT_CONFIGURED_MESSAGE
        ) from exc
    try:
        return store.create_summary(draft)
    except SummaryStoreConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Summary already exists: {draft.summary_id}",
        ) from exc


@staff_router.post(
    "/{summary_id}/approve",
    response_model=SummaryDraft,
    summary="Approve a sourced summary for signed-record export",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={
        404: {"description": "Summary not found"},
        409: {"description": "Summary changed or is no longer pending; reload before approving"},
    },
)
def approve_summary(
    request: Request,
    summary_id: str,
    payload: SummaryApprovalRequest,
    store: SummaryStore = Depends(get_summary_store),
    reviews: CaptionReviewStore = Depends(get_caption_review_store),
) -> SummaryDraft:
    current = store.get_summary(summary_id)
    if current is not None and current.agenda_minutes is not None:
        _current_minutes_cues(current, reviews)
    operator_identity = request.state.operator_identity
    approval = OperatorApproval(
        summary_id=summary_id,
        operator_id=operator_identity.operator_id,
        operator_display_name=operator_identity.operator_display_name,
        approved_at=datetime.now(UTC),
        approval_note=payload.approval_note,
    )
    try:
        return store.approve_summary(
            approval, expected_audit_fingerprint=payload.expected_audit_fingerprint
        )
    except SummaryStoreNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Summary not found: {summary_id}. Generate or select an existing summary first.",
        ) from exc
    except SummaryStoreConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.post(
    "/transcript.csv",
    summary="Export committed transcript cues as CSV",
    dependencies=[Depends(require_any_role("records_clerk", "support_admin"))],
    responses={200: {"content": {"text/csv": {}}}},
)
def export_transcript_csv(payload: SummaryGenerateRequest) -> Response:
    return Response(content=render_transcript_csv(payload.cues), media_type="text/csv")


# --- async summary generation job (field evidence 2026-08-29) ---------------
#
# POST /generate above blocks the request for as long as the model takes -- fine
# for a fast cloud tier, but a legitimate CPU-only local generation (measured
# 94-366s+ on the 32GB reference station) either sits behind an operator's open
# browser tab for minutes or, pre-fix, got discarded when the control plane's own
# timeout fired before Ollama finished. These endpoints are the async path: queue
# the same generation as a durable job (civiccast/summary/job.py, the same pattern
# civiccast.captions.vod_job established for offline captioning) and let the
# operator console poll visible progress instead of holding the connection open.
# POST /generate is unchanged and still works for a fast tier or a script.


@staff_router.post(
    "/jobs",
    response_model=SummaryGenerationJobRecord,
    status_code=status.HTTP_201_CREATED,
    summary="Queue async summary generation from committed transcript cues",
    dependencies=[Depends(require_any_role("records_clerk", "support_admin"))],
    responses={503: {"description": _SUMMARY_JOB_STORE_NOT_READY_DETAIL}},
)
def create_summary_job(
    payload: SummaryGenerateRequest,
    store: SummaryGenerationJobStore | None = Depends(get_summary_job_store),
) -> SummaryGenerationJobRecord:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_SUMMARY_JOB_STORE_NOT_READY_DETAIL,
        )
    return enqueue_summary_job(store, meeting_id=payload.meeting_id, cues=payload.cues)


@staff_router.get(
    "/jobs",
    response_model=list[SummaryGenerationJobRecord],
    summary="List summary generation jobs for operator visibility",
    responses={503: {"description": _SUMMARY_JOB_STORE_NOT_READY_DETAIL}},
)
def list_summary_jobs(
    meeting_id: str | None = None,
    state: SummaryGenerationJobState | None = None,
    store: SummaryGenerationJobStore | None = Depends(get_summary_job_store),
) -> list[SummaryGenerationJobRecord]:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_SUMMARY_JOB_STORE_NOT_READY_DETAIL,
        )
    return store.list(meeting_id=meeting_id, state=state)


@staff_router.get(
    "/jobs/{job_id}",
    response_model=SummaryGenerationJobRecord,
    summary="Get one summary generation job's status/progress",
    responses={
        404: {"description": "Summary generation job not found"},
        503: {"description": _SUMMARY_JOB_STORE_NOT_READY_DETAIL},
    },
)
def get_summary_job(
    job_id: str,
    store: SummaryGenerationJobStore | None = Depends(get_summary_job_store),
) -> SummaryGenerationJobRecord:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_SUMMARY_JOB_STORE_NOT_READY_DETAIL,
        )
    job = store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Summary generation job not found: {job_id}",
        )
    return job


@staff_router.post(
    "/jobs/{job_id}/retry",
    response_model=SummaryGenerationJobRecord,
    summary="Manually retry a failed summary generation job",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={
        404: {"description": "Summary generation job not found"},
        409: {"description": "Only a failed job can be manually retried"},
        503: {"description": _SUMMARY_JOB_STORE_NOT_READY_DETAIL},
    },
)
def retry_summary_job(
    job_id: str,
    store: SummaryGenerationJobStore | None = Depends(get_summary_job_store),
) -> SummaryGenerationJobRecord:
    """Reset a failed job to pending with a fresh attempt budget.

    Mirrors ``captions.router.retry_offline_caption_job``: pre-checks
    ``active_for_meeting`` so a retry never creates a second active job for a
    meeting that picked up a new job in the meantime (a second operator's retry,
    or a fresh generate), and catches the same conflict error the durable store's
    ``save`` raises if that check loses a TOCTOU race, so a race never surfaces as
    a raw 500.
    """
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_SUMMARY_JOB_STORE_NOT_READY_DETAIL,
        )
    job = store.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Summary generation job not found: {job_id}",
        )
    if job.state != SUMMARY_JOB_STATE_FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Summary generation job {job_id} is {job.state!r}; only a failed "
                "job can be manually retried."
            ),
        )
    conflict = store.active_for_meeting(job.meeting_id)
    if conflict is not None and conflict.job_id != job_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Summary generation job {conflict.job_id!r} is already active for "
                f"meeting {job.meeting_id!r}; retry {job_id} once it finishes."
            ),
        )
    now = datetime.now(UTC)
    try:
        return store.save(
            job.model_copy(
                update={
                    "state": SUMMARY_JOB_STATE_PENDING,
                    "attempts": 0,
                    "next_attempt_at": now,
                    "last_error": "",
                    "updated_at": now,
                }
            )
        )
    except SummaryGenerationJobConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Summary generation job {job_id} could not be retried: another job "
                f"is now active for meeting {job.meeting_id!r}."
            ),
        ) from exc


@staff_router.post(
    "/agendas/{agenda_id}/minutes",
    response_model=SummaryDraft,
    status_code=201,
    summary="Draft agenda minutes from a cited summary or start a manual draft",
    dependencies=[Depends(require_any_role("records_clerk"))],
)
def create_agenda_minutes(
    agenda_id: str,
    payload: AgendaMinutesCreateRequest,
    agendas: AgendaStore | None = Depends(get_agenda_store),
    agenda_service: AgendaService | None = Depends(get_agenda_service),
    recordings: Any = Depends(get_recording_store),
    reviews: CaptionReviewStore = Depends(get_caption_review_store),
    store: SummaryStore = Depends(get_summary_store),
) -> SummaryDraft:
    if agendas is None:
        raise HTTPException(
            503, "Agenda storage is unavailable. Restore station storage and retry."
        )
    agenda = agendas.get_agenda(agenda_id)
    if agenda is None:
        raise HTTPException(404, "Agenda not found.")
    job = (
        recordings.get_job(agenda.source_recording_job_id)
        if recordings and agenda.source_recording_job_id
        else None
    )
    asset_id = job.asset_id if job and job.asset_id else agenda.meeting_asset_id
    reviewed = reviewed_caption_cues(reviews, asset_id)
    if reviewed.pending or not reviewed.cues:
        raise HTTPException(
            409, "Finish English caption review for this recording before creating cited minutes."
        )
    items = agendas.list_items(agenda_id, order_by="order")
    if not items:
        raise HTTPException(409, "Add agenda items before drafting minutes.")
    source = store.get_summary(payload.source_summary_id) if payload.source_summary_id else None
    if payload.source_summary_id and (
        source is None
        or source.meeting_id != asset_id
        or source.status == "refused"
        or source.agenda_minutes
    ):
        raise HTTPException(
            409, "Select a completed cited summary for this recording, or start a manual draft."
        )
    if source and source.provenance.runtime_parameters.get(
        "source_transcript_fingerprint"
    ) != transcript_fingerprint(reviewed.cues):
        raise HTTPException(
            409,
            "The summary lacks current reviewed-transcript provenance. Regenerate it after caption corrections, or start a manual draft.",
        )
    window = agenda_service.source_playback_window(asset_id) if agenda_service else None
    offset = window[0] if window else 0.0
    timed = sorted(
        [
            (
                item.video_timecode_s + (0 if agenda.source_recording_job_id else offset),
                item.item_id,
            )
            for item in items
            if item.video_timecode_s is not None
        ]
    )
    sections = {
        item.item_id: MinutesSection(item_id=item.item_id, title=item.title) for item in items
    }
    if source:
        for claim in source.sourced_claims:
            if not claim.transcript_ranges:
                continue
            point = claim.transcript_ranges[0].start_seconds
            owners = [identity for start, identity in timed if start <= point]
            if not owners:
                continue
            section = sections[owners[-1]]
            sections[section.item_id] = section.model_copy(
                update={
                    "discussion": (section.discussion + "\n" + claim.text).strip(),
                    "transcript_ranges": [*section.transcript_ranges, *claim.transcript_ranges],
                }
            )
    content = MinutesContent(sections=list(sections.values()))
    try:
        validate_minutes_sources(content, reviewed.cues)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    minutes = AgendaMinutes(
        agenda_id=agenda_id,
        asset_id=asset_id,
        source_summary_id=source.summary_id if source else None,
        source_summary_fingerprint=source.audit_fingerprint if source else None,
        transcript_fingerprint=transcript_fingerprint(reviewed.cues),
        playback_start_seconds=window[0] if window else None,
        playback_end_seconds=window[1] if window else None,
        content=content,
    )
    summary = SummaryDraft(
        summary_id=f"minutes-{uuid4().hex}",
        meeting_id=asset_id,
        status="pending_review",
        narrative=render_minutes(minutes),
        sourced_claims=[],
        provenance=source.provenance
        if source
        else ModelProvenance(
            model_tag="manual-no-model",
            prompt_version="manual-agenda-minutes",
            extraction_version="reviewed-english-cues",
            generated_at=datetime.now(UTC),
        ),
        audit_fingerprint=sha256_fingerprint({"minutes": minutes.model_dump(mode="json")}),
        agenda_minutes=minutes,
        operator_message="Draft only. Copied cited discussion follows timed agenda items; empty/unassigned items need manual drafting. Enter and confirm motions/votes; no AI decision is official.",
    )
    return store.create_summary(summary)


def _current_minutes_cues(summary: SummaryDraft, reviews: CaptionReviewStore) -> list[CaptionCue]:
    assert summary.agenda_minutes is not None
    reviewed = reviewed_caption_cues(reviews, summary.agenda_minutes.asset_id)
    if (
        reviewed.pending
        or transcript_fingerprint(reviewed.cues) != summary.agenda_minutes.transcript_fingerprint
    ):
        raise HTTPException(
            409,
            "The reviewed transcript changed. Rebuild the draft minutes from current cues before approval.",
        )
    try:
        validate_minutes_sources(summary.agenda_minutes.content, reviewed.cues)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return reviewed.cues


@staff_router.patch(
    "/{summary_id}/minutes",
    response_model=SummaryDraft,
    summary="Edit sourced agenda discussion and human-entered motions/votes",
    dependencies=[Depends(require_any_role("records_clerk"))],
)
def edit_agenda_minutes(
    request: Request,
    summary_id: str,
    payload: AgendaMinutesEditRequest,
    store: SummaryStore = Depends(get_summary_store),
    reviews: CaptionReviewStore = Depends(get_caption_review_store),
) -> SummaryDraft:
    summary = store.get_summary(summary_id)
    if summary is None or summary.agenda_minutes is None:
        raise HTTPException(404, "Agenda minutes not found.")
    cues = _current_minutes_cues(summary, reviews)
    if [(s.item_id, s.title) for s in payload.content.sections] != [
        (s.item_id, s.title) for s in summary.agenda_minutes.content.sections
    ]:
        raise HTTPException(
            422, "Agenda identity/order is fixed in this draft. Rebuild after agenda changes."
        )
    try:
        validate_minutes_sources(payload.content, cues)
        return store.edit_minutes(
            summary_id,
            content=payload.content,
            operator_id=request.state.operator_identity.operator_id,
            expected_audit_fingerprint=payload.expected_audit_fingerprint,
        )
    except SummaryStoreConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
