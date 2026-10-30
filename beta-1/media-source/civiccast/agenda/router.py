# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S25 meeting-agenda API surface (slice 3).

Two surface families over the ``AgendaStore`` + ``AgendaService`` DI seams:

* **Staff CRUD** (``/api/staff/agendas[/{agenda_id}][/items[/{item_id}]]``,
  ``/sync-from-chapters``, ``/import``) — ``records_clerk`` /
  ``meeting_operator`` are the spec §4 AUTHOR roles. Status flips ride
  PATCH /agendas/{id} so the publish gate (slice 2 ``AgendaService.publish``)
  runs unconditionally — a generic PATCH can never bypass the empty-agenda
  refusal (DC-1 / DC-6).
* **Public read** (``/api/public/agendas/{meeting_asset_id}``) — current viewer access;
  the service returns ``None`` for missing / draft agendas and the router
  translates that to 404 (DC-6 cornerstone — drafts MUST NOT reach
  viewers).

DI seams (``get_agenda_store`` / ``get_agenda_service``) return ``None`` at
import so the module opens no database; the app factory overrides them in
``_wire_durable_stores``. An unwired surface is a 503 — never a silent 200
against storage that is not there. ``x-required-roles`` is mirrored into the
generated OpenAPI so the published contract cannot drift from the runtime
role gate (same convention as S23 / S24).
"""

from __future__ import annotations

import functools
import hashlib
import math
import os
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from civiccast.agenda.models import (
    AgendaChapterSyncResult,
    AgendaItem,
    AgendaItemInput,
    AgendaItemUpdate,
    AgendaStatus,
    MeetingAgenda,
    MeetingAgendaInput,
    MeetingAgendaUpdate,
    PublicMeetingAgenda,
)
from civiccast.agenda.service import (
    AgendaImportDecodeError,
    AgendaImportNoItemsError,
    AgendaPublishError,
    AgendaService,
    AgendaServiceError,
)
from civiccast.agenda.store import (
    AgendaItemNotFoundError,
    AgendaItemOrderConflictError,
    AgendaNotFoundError,
    AgendaStore,
    AgendaUniqueViolationError,
)
from civiccast.auth.roles import require_any_role
from civiccast.metadata.router import _transcript_access
from civiccast.paywall.router import get_paywall_service
from civiccast.recording.router import get_recording_service, get_recording_store
from civiccast.schedule.router import get_asset_store

_DB_NOT_READY = "Durable storage is not ready yet."

# Spec §4 — the AUTHOR roles. ``records_clerk`` + ``meeting_operator`` are
# the only staff scopes that can CRUD agendas / items, sync, or import.
_AUTHOR = ("records_clerk", "meeting_operator")
_AUTHOR_EXTRA = {"x-required-roles": list(_AUTHOR)}

_DEFAULT_STATION_ID = "civiccast-station"


@functools.cache
def _station_id() -> str:
    """The active station id (single-station deployment; env-overridable).

    Cached because the station id is process-fixed in the deployed posture
    — re-reading ``os.environ`` on every public read was a hot-path nit
    (Q-5). Tests that need a different station id should set the env BEFORE
    importing this module or call ``_station_id.cache_clear()`` in a fixture.
    """
    return os.environ.get("CIVICCAST_STATION_ID") or _DEFAULT_STATION_ID


# --- DI seams (overridden by the app factory) --------------------------------


def get_agenda_store() -> AgendaStore | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def get_agenda_service() -> AgendaService | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def _require_store(store: AgendaStore | None) -> AgendaStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_service(svc: AgendaService | None) -> AgendaService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


# --- routers -----------------------------------------------------------------

staff_router = APIRouter(prefix="/api/staff", tags=["staff", "agenda"])
public_router = APIRouter(prefix="/api/public", tags=["public", "agenda"])


class AgendaMeetingOption(BaseModel):
    meeting_asset_id: str
    title: str
    source_recording_job_id: str | None = None
    state: str
    duration_seconds: float | None = None
    can_mark_now: bool = False


def _recording_reference(job_id: str) -> str:
    return "recording-" + hashlib.sha256(job_id.encode()).hexdigest()[:32]


@staff_router.get(
    "/agenda-meetings",
    response_model=list[AgendaMeetingOption],
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
)
def agenda_meetings(
    assets: Any = Depends(get_asset_store), recordings: Any = Depends(get_recording_store)
) -> list[AgendaMeetingOption]:
    if assets is None:
        raise HTTPException(503, _DB_NOT_READY)
    jobs = recordings.list_jobs(_station_id()) if recordings is not None else []
    by_asset = {job.asset_id: job for job in jobs if job.asset_id}
    options = []
    for asset in assets.list_all():
        job = by_asset.get(asset.asset_id)
        options.append(
            AgendaMeetingOption(
                meeting_asset_id=asset.asset_id,
                title=asset.title,
                source_recording_job_id=job.job_id if job else None,
                state=asset.state,
                duration_seconds=asset.duration_seconds,
            )
        )
    for job in jobs:
        if job.asset_id or job.state in {"failed", "skipped", "done"}:
            continue
        schedule = recordings.get_schedule(job.schedule_id) if job.schedule_id else None
        name = schedule.name if schedule else (job.target_series or "Recording")
        options.append(
            AgendaMeetingOption(
                meeting_asset_id=_recording_reference(job.job_id),
                title=f"{name} — {job.planned_start.strftime('%Y-%m-%d %H:%M')}",
                source_recording_job_id=job.job_id,
                state=job.state,
                can_mark_now=job.state == "recording",
            )
        )
    return sorted(options, key=lambda option: option.title.casefold())


def _validate_marker(
    agenda: MeetingAgenda,
    point: int | None,
    assets: Any,
    recordings: Any,
    capture: Any,
    service: AgendaService | None,
) -> None:
    if point is None:
        return
    if assets is None:
        raise HTTPException(503, _DB_NOT_READY)
    job = (
        recordings.get_job(agenda.source_recording_job_id)
        if recordings is not None and agenda.source_recording_job_id
        else None
    )
    asset = assets.get_staff_row(job.asset_id if job and job.asset_id else agenda.meeting_asset_id)
    duration = asset.duration_seconds if asset is not None else None
    if job and job.state == "recording":
        if capture is None:
            raise HTTPException(503, "Recording time is unavailable; retry after capture recovers.")
        try:
            duration = capture.recorded_position_seconds(job.job_id)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
    elif duration is None and job:
        duration = (job.planned_end - job.planned_start).total_seconds()
    if not agenda.source_recording_job_id and service is not None:
        playback_duration = service.playback_duration_seconds(agenda.meeting_asset_id)
        if playback_duration is not None:
            duration = playback_duration
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise HTTPException(
            409,
            "Recording duration is unavailable. Wait for finalization before setting item times.",
        )
    if point >= duration:
        raise HTTPException(422, "Item time must be within the recorded video duration.")


@staff_router.post(
    "/agendas/{agenda_id}/items/{item_id}/mark-now",
    response_model=AgendaItem,
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
)
def mark_item_now(
    agenda_id: str,
    item_id: str,
    store: AgendaStore | None = Depends(get_agenda_store),
    capture: Any = Depends(get_recording_service),
) -> AgendaItem:
    resolved = _require_store(store)
    agenda = resolved.get_agenda(agenda_id)
    item = resolved.get_item(item_id)
    if agenda is None or item is None or item.agenda_id != agenda_id:
        raise HTTPException(404, "Agenda item not found.")
    if not agenda.source_recording_job_id:
        raise HTTPException(
            409, "This agenda is not linked to a current recording. Enter a reviewed time instead."
        )
    if capture is None:
        raise HTTPException(503, "Recording service is unavailable.")
    try:
        point = capture.recorded_position_seconds(agenda.source_recording_job_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not math.isfinite(point) or point <= 0:
        raise HTTPException(409, "Recording time is not available yet.")
    return resolved.upsert_item(
        item.model_copy(update={"video_timecode_s": max(0, math.floor(point - 0.001))})
    )


# --- agenda CRUD -------------------------------------------------------------


@staff_router.get(
    "/agendas",
    response_model=list[MeetingAgenda],
    summary="List meeting agendas for the station",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_agendas(
    status_filter: AgendaStatus | None = Query(
        None, alias="status", description="Filter to one status (draft / published)"
    ),
    store: AgendaStore | None = Depends(get_agenda_store),
) -> list[MeetingAgenda]:
    """All agendas for the active station, ordered by meeting asset id then
    agenda id. Optional ``status`` narrows to draft or published only."""
    return _require_store(store).list_agendas(_station_id(), status=status_filter)


@staff_router.post(
    "/agendas",
    response_model=MeetingAgenda,
    status_code=status.HTTP_201_CREATED,
    summary="Create a meeting agenda (always draft)",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        409: {"description": "Meeting agenda with this agenda_id already exists"},
        503: {"description": _DB_NOT_READY},
    },
)
def create_agenda(
    payload: MeetingAgendaInput,
    store: AgendaStore | None = Depends(get_agenda_store),
    recordings: Any = Depends(get_recording_store),
) -> MeetingAgenda:
    """Create one agenda in draft status. Publishing rides PATCH
    ``status=published`` so the empty-agenda gate (slice 2) runs.

    Q-4 parity: a POST that targets an existing ``agenda_id`` returns 409
    Conflict — the store would otherwise upsert silently, retroactively
    overwriting another meeting's agenda when an operator typos the id. Use
    PATCH to update an existing agenda."""
    resolved = _require_store(store)
    if resolved.get_agenda(payload.agenda_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Meeting agenda {payload.agenda_id!r} already exists. Use PATCH to update.",
        )
    if payload.source_recording_job_id:
        job = recordings.get_job(payload.source_recording_job_id) if recordings else None
        if job is None or job.station_id != _station_id():
            raise HTTPException(404, "Recording not found.")
        payload = payload.model_copy(
            update={"meeting_asset_id": job.asset_id or _recording_reference(job.job_id)}
        )
        if resolved.get_agenda_by_recording_job(_station_id(), job.job_id):
            raise HTTPException(409, "An agenda already exists for this recording.")
    if resolved.get_agenda_by_asset(_station_id(), payload.meeting_asset_id):
        raise HTTPException(409, "An agenda already exists for this recording.")
    try:
        return resolved.upsert_agenda(MeetingAgenda(**payload.model_dump()))
    except AgendaUniqueViolationError as exc:
        # Two operators racing to create an agenda for the same
        # (station_id, meeting_asset_id) — controlled 409, never a raw 500.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/agendas/{agenda_id}",
    response_model=MeetingAgenda,
    summary="Get one meeting agenda",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Meeting agenda not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def get_agenda(
    agenda_id: str,
    store: AgendaStore | None = Depends(get_agenda_store),
) -> MeetingAgenda:
    agenda = _require_store(store).get_agenda(agenda_id)
    if agenda is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Meeting agenda {agenda_id!r} not found."
        )
    return agenda


@staff_router.patch(
    "/agendas/{agenda_id}",
    response_model=MeetingAgenda,
    summary="Patch a meeting agenda (absent keys unchanged; status flips through the publish gate)",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Meeting agenda not found"},
        422: {"description": "Publish refused (e.g. agenda has zero items)"},
        503: {"description": _DB_NOT_READY},
    },
)
def patch_agenda(
    agenda_id: str,
    payload: MeetingAgendaUpdate,
    store: AgendaStore | None = Depends(get_agenda_store),
    svc: AgendaService | None = Depends(get_agenda_service),
) -> MeetingAgenda:
    """Patch an agenda.

    Patch semantics: an absent key leaves the stored field unchanged; an
    explicit ``null`` clears the field (e.g. ``{"source_doc_url": null}`` sets
    ``source_doc_url`` to ``None``). ``agenda_id`` / ``station_id`` /
    ``meeting_asset_id`` are set at creation and not editable here.

    Status: when ``status`` is present, the PATCH calls
    ``AgendaService.publish`` / ``unpublish`` instead of a generic field
    update, so the publish gate runs (DC-1 — refuses to publish a zero-item
    agenda with 422)."""
    resolved_store = _require_store(store)
    current = resolved_store.get_agenda(agenda_id)
    if current is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Meeting agenda {agenda_id!r} not found."
        )
    updates = payload.model_dump(exclude_unset=True)

    # Status flips go through the service so the empty-agenda gate runs.
    requested_status = updates.pop("status", None)
    if requested_status is not None:
        resolved_svc = _require_service(svc)
        try:
            if requested_status == "published":
                current = resolved_svc.publish(agenda_id)
            else:
                current = resolved_svc.unpublish(agenda_id)
        except AgendaPublishError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        except AgendaNotFoundError as exc:  # pragma: no cover - guarded above
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # Remaining field updates (source_doc_url). model_copy preserves field
    # identity so the explicit ``null`` clears semantics survive.
    if updates:
        merged = current.model_copy(update=updates)
        try:
            current = resolved_store.upsert_agenda(merged)
        except AgendaUniqueViolationError as exc:  # pragma: no cover - identity fields immutable
            raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return current


@staff_router.delete(
    "/agendas/{agenda_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a meeting agenda (cascades items)",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Meeting agenda not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_agenda(
    agenda_id: str,
    store: AgendaStore | None = Depends(get_agenda_store),
) -> Response:
    """Delete an agenda AND every item that referenced it.

    The cascade is performed in a single transaction by the store (loose-ref
    convention — no DB foreign key would otherwise catch the orphan). 404 if
    the agenda does not exist; never a silent no-op."""
    try:
        _require_store(store).delete_agenda(agenda_id)
    except AgendaNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- item CRUD ---------------------------------------------------------------


_ItemOrderBy = Literal["order", "timecode"]


@staff_router.get(
    "/agendas/{agenda_id}/items",
    response_model=list[AgendaItem],
    summary="List agenda items (default: ordered by operator-set order; timecode for the player)",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Meeting agenda not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def list_items(
    agenda_id: str,
    order_by: _ItemOrderBy = Query(
        "order",
        description="`order` for the agenda sidebar; `timecode` for the player chapter list.",
    ),
    store: AgendaStore | None = Depends(get_agenda_store),
) -> list[AgendaItem]:
    """Items for an agenda. ``order_by="order"`` for the editor / sidebar,
    ``order_by="timecode"`` for the player chapter list (NULL timecodes last
    in both)."""
    resolved = _require_store(store)
    if resolved.get_agenda(agenda_id) is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Meeting agenda {agenda_id!r} not found."
        )
    return resolved.list_items(agenda_id, order_by=order_by)


@staff_router.post(
    "/agendas/{agenda_id}/items",
    response_model=AgendaItem,
    status_code=status.HTTP_201_CREATED,
    summary="Create an agenda item",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Meeting agenda not found"},
        409: {
            "description": (
                "Agenda item with this item_id already exists, OR another item already "
                "occupies (agenda_id, order)."
            )
        },
        503: {"description": _DB_NOT_READY},
    },
)
def create_item(
    agenda_id: str,
    payload: AgendaItemInput,
    store: AgendaStore | None = Depends(get_agenda_store),
    assets: Any = Depends(get_asset_store),
    recordings: Any = Depends(get_recording_store),
    capture: Any = Depends(get_recording_service),
    svc: AgendaService | None = Depends(get_agenda_service),
) -> AgendaItem:
    """Create one item under ``agenda_id``.

    The path ``agenda_id`` is authoritative — the body's ``agenda_id`` must
    match (422 otherwise so the operator can't accidentally write an item
    against a different agenda).

    Q-4 parity: a POST that targets an existing ``item_id`` returns 409
    Conflict; use PATCH to update. A POST whose ``(agenda_id, order)``
    collides with another item is ALSO a 409 — two operators racing to
    drop items at the same slot get a controlled conflict, never a raw
    500 (E-2 / Q-2 / T-1)."""
    resolved = _require_store(store)
    agenda = resolved.get_agenda(agenda_id)
    if agenda is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Meeting agenda {agenda_id!r} not found."
        )
    if payload.agenda_id != agenda_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Body agenda_id {payload.agenda_id!r} does not match path agenda_id {agenda_id!r}."
            ),
        )
    if resolved.get_item(payload.item_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Agenda item {payload.item_id!r} already exists. Use PATCH to update.",
        )
    _validate_marker(agenda, payload.video_timecode_s, assets, recordings, capture, svc)
    try:
        return resolved.upsert_item(AgendaItem(**payload.model_dump()))
    except AgendaItemOrderConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.patch(
    "/agendas/{agenda_id}/items/{item_id}",
    response_model=AgendaItem,
    summary="Patch an agenda item (absent keys unchanged)",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Agenda item not found"},
        409: {"description": "Another item already occupies (agenda_id, order)"},
        503: {"description": _DB_NOT_READY},
    },
)
def patch_item(
    agenda_id: str,
    item_id: str,
    payload: AgendaItemUpdate,
    store: AgendaStore | None = Depends(get_agenda_store),
    assets: Any = Depends(get_asset_store),
    recordings: Any = Depends(get_recording_store),
    capture: Any = Depends(get_recording_service),
    svc: AgendaService | None = Depends(get_agenda_service),
) -> AgendaItem:
    """Patch semantics: absent key unchanged, explicit ``null`` clears.
    ``item_id`` / ``agenda_id`` are set at creation. A PATCH that moves the
    item to an ``order`` already occupied by another item is a 409
    (E-2 / Q-2 / T-1)."""
    resolved = _require_store(store)
    current = resolved.get_item(item_id)
    if current is None or current.agenda_id != agenda_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Agenda item {item_id!r} not found.")
    updates = payload.model_dump(exclude_unset=True)
    merged = current.model_copy(update=updates)
    if "video_timecode_s" in updates:
        agenda = resolved.get_agenda(agenda_id)
        assert agenda is not None
        _validate_marker(agenda, merged.video_timecode_s, assets, recordings, capture, svc)
    try:
        return resolved.upsert_item(merged)
    except AgendaItemOrderConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.delete(
    "/agendas/{agenda_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an agenda item",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Agenda item not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_item(
    agenda_id: str,
    item_id: str,
    store: AgendaStore | None = Depends(get_agenda_store),
) -> Response:
    resolved = _require_store(store)
    current = resolved.get_item(item_id)
    if current is None or current.agenda_id != agenda_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Agenda item {item_id!r} not found.")
    try:
        resolved.delete_item(item_id)
    except AgendaItemNotFoundError as exc:  # pragma: no cover - guarded above
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- sync + import -----------------------------------------------------------


@staff_router.post(
    "/agendas/{agenda_id}/sync-from-chapters",
    response_model=AgendaChapterSyncResult,
    summary="Seed agenda items from the meeting asset's chapter markers",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Meeting agenda not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def sync_from_chapters(
    agenda_id: str,
    svc: AgendaService | None = Depends(get_agenda_service),
) -> AgendaChapterSyncResult:
    """Project ``Asset.chapters_json`` for the agenda's meeting asset into
    draft items (DC-3). Idempotent: items at an existing ``(agenda_id, order)``
    are skipped — operator edits survive a re-sync. Returns the items the
    service wrote on this call, and identifies chapter markers skipped because
    an agenda item already occupies their ordered position."""
    resolved = _require_service(svc)
    try:
        return resolved.sync_from_chapters(agenda_id)
    except AgendaNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AgendaServiceError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@staff_router.post(
    "/agendas/{agenda_id}/import",
    response_model=list[AgendaItem],
    summary="Best-effort import of an uploaded agenda doc (text/plain or PDF)",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Meeting agenda not found"},
        415: {
            "description": "Unsupported content type (only text/plain and application/pdf are parsed)"
        },
        422: {"description": "PDF was readable but no recognizable agenda items were found in it"},
        503: {"description": _DB_NOT_READY},
    },
)
async def import_from_doc(
    agenda_id: str,
    request: Request,
    svc: AgendaService | None = Depends(get_agenda_service),
) -> list[AgendaItem]:
    """Parse the request body as an agenda doc and seed draft items.

    Body: the doc bytes (operators ``POST`` the file content with the right
    ``Content-Type``). ``text/plain`` is parsed literally, line by line.
    ``application/pdf`` runs the heuristic text-layer extractor in
    ``civiccast.agenda.pdf_import`` (numbered items, ALL-CAPS section
    headings, standalone time markers); each returned item carries a
    ``confidence`` score, and if the target agenda is currently
    ``published`` the import reopens it to ``draft`` so an operator must
    review and republish before the new, heuristically-guessed items reach
    the public portal (AI/agenda non-negotiables Spec Sec4.2). Any other
    content type returns 415 Unsupported Media Type. Idempotent on re-run
    via the same skip-by-order rule ``sync_from_chapters`` uses.

    The endpoint REQUIRES a ``Content-Type`` header (E-3 / Q-1). A missing
    header is a 415 — silently defaulting to ``text/plain`` masked binary
    uploads as text and surfaced as raw 500 on the decode. Bodies that aren't
    valid UTF-8 are also a 415 — same root cause. A readable PDF with zero
    recognizable lines is a 422, not a silent empty import."""
    resolved = _require_service(svc)
    body = await request.body()
    content_type = request.headers.get("content-type")
    if content_type is None:
        # The router exists to translate "not text/plain or application/pdf"
        # into 415; a missing header sidestepped that translation. Make the
        # contract explicit instead of silently labeling the body text/plain.
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "Content-Type header is required for /import; only 'text/plain' and "
                "'application/pdf' are parsed."
            ),
        )
    try:
        return resolved.import_from_doc(agenda_id, doc_bytes=body, content_type=content_type)
    except AgendaNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AgendaImportDecodeError as exc:
        # The body declared text/plain but the bytes aren't UTF-8 — 415
        # with a structured diagnostic, never a raw 500 (E-3 / Q-1).
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc
    except AgendaImportNoItemsError as exc:
        # The PDF was readable and the content type is supported, but the
        # heuristic extractor's disclosed ceiling found nothing reliable —
        # 422 (unprocessable), distinct from the 415 "wrong format" cases.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except NotImplementedError as exc:
        # DOCX / anything not text/plain or application/pdf — surface as 415
        # so the operator gets the right diagnostic, not a generic 500.
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc


# --- public read -------------------------------------------------------------


@public_router.get(
    "/agendas/{meeting_asset_id}",
    response_model=PublicMeetingAgenda,
    summary="Public read: published meeting agenda for one meeting asset",
    responses={
        403: {"description": "Viewer access required"},
        404: {"description": "No published agenda for this meeting asset"},
        503: {"description": _DB_NOT_READY},
    },
)
def get_public_agenda(
    meeting_asset_id: str,
    request: Request,
    response: Response,
    svc: AgendaService | None = Depends(get_agenda_service),
    paywall: Any = Depends(get_paywall_service),
) -> PublicMeetingAgenda:
    """Public projection: only ``status="published"`` agendas surface.

    DC-6 cornerstone: a draft (or missing) agenda is a 404 here — viewers
    must NEVER see a draft. The service returns ``None`` in both cases; the
    router translates that to 404 so the public surface cannot distinguish
    "no agenda" from "draft only" (no probing surface)."""
    resolved = _require_service(svc)
    view = resolved.public_view(_station_id(), meeting_asset_id)
    if view is None:
        # Q-4 — DO NOT echo the user-supplied ``meeting_asset_id`` here. The
        # path the requester sent already tells them what they asked for;
        # reflecting it into the body is a tiny amplification + a needless
        # privacy surface compared with a fixed string.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="Meeting agenda not found.",
        )
    if paywall is None:
        raise HTTPException(503, "Playback access policy is unavailable.")
    if not _transcript_access(request, paywall, meeting_asset_id):
        raise HTTPException(403, "This recording requires viewer access.")
    response.headers["Cache-Control"] = "no-store"
    return view


__all__ = [
    "get_agenda_service",
    "get_agenda_store",
    "public_router",
    "staff_router",
]
