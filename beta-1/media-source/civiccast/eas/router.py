# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI staff routes for the EAS ingest+display console (S11c).

Staff-only (``/api/staff/eas``); there is NO public router here — public display goes
through the existing ``/api/public/cg/emergency-overlay`` endpoint, which never labels
content "EAS". Source CRUD + display actions are role-gated; display actions read the
operator id from the verified token. A ``forced_slate`` is refused unless the request is
operator-confirmed (decision 3 — CivicCast never auto-preempts; it is not an EAS device).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import require_any_role
from civiccast.eas.cap import stable_alert_id
from civiccast.eas.models import (
    EasCapAlert,
    EasCapSource,
    EasDisplayDecision,
    EasDisplayMode,
    EasSeverity,
    EasSourceKind,
)
from civiccast.eas.service import EasDisplayError, EasDisplayService
from civiccast.eas.store import AlertNotFoundError, EasStore, EasStoreError

_DB_NOT_READY = "Durable storage is not ready yet."

_READ = ("setup_admin", "support_admin", "meeting_operator")
_SOURCE_WRITE = ("setup_admin",)
_DISPLAY = ("setup_admin", "meeting_operator")

staff_router = APIRouter(prefix="/api/staff/eas", tags=["staff", "eas"])


# --- DI seams (overridden by the app factory) -------------------------------


def get_eas_store() -> EasStore | None:
    return None


def get_eas_service() -> EasDisplayService | None:
    return None


def _require_store(store: EasStore | None) -> EasStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_service(svc: EasDisplayService | None) -> EasDisplayService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


def _operator_id(request: Request) -> str:
    identity = getattr(request.state, "operator_identity", None)
    if not isinstance(identity, OperatorIdentity):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Staff identity is required for this action."
        )
    return identity.operator_id


# --- request bodies ----------------------------------------------------------


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    kind: EasSourceKind
    endpoint_url: Annotated[str | None, Field(default=None, max_length=1000)] = None
    geocode_filter: list[str] = Field(default_factory=list)
    severity_floor: EasSeverity = "severe"
    poll_seconds: Annotated[int, Field(ge=15, le=3600)] = 60
    enabled: bool = True
    credential_ref: Annotated[str | None, Field(default=None, max_length=200)] = None
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None


class ManualAlertInput(BaseModel):
    """An operator-entered alert (source kind ``manual``)."""

    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    identifier: Annotated[str, Field(min_length=1, max_length=255)]
    event: Annotated[str, Field(min_length=1, max_length=255)]
    severity: EasSeverity
    headline: Annotated[str | None, Field(default=None, max_length=500)] = None
    description: Annotated[str | None, Field(default=None)] = None
    instruction: Annotated[str | None, Field(default=None)] = None
    areas: list[str] = Field(default_factory=list)
    expires: datetime | None = None


class DisplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    mode: EasDisplayMode
    operator_confirmed: bool = False


# --- sources -----------------------------------------------------------------


@staff_router.get(
    "/sources",
    response_model=list[EasCapSource],
    dependencies=[Depends(require_any_role(*_READ))],
)
def list_sources(store: EasStore | None = Depends(get_eas_store)) -> list[EasCapSource]:
    return _require_store(store).list_sources()


@staff_router.put(
    "/sources/{source_id}",
    response_model=EasCapSource,
    dependencies=[Depends(require_any_role(*_SOURCE_WRITE))],
)
def upsert_source(
    source_id: str, payload: SourceInput, store: EasStore | None = Depends(get_eas_store)
) -> EasCapSource:
    if payload.source_id != source_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="source_id mismatch.")
    return _require_store(store).upsert_source(EasCapSource(**payload.model_dump()))


@staff_router.delete(
    "/sources/{source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_SOURCE_WRITE))],
)
def delete_source(source_id: str, store: EasStore | None = Depends(get_eas_store)) -> None:
    try:
        _require_store(store).delete_source(source_id)
    except EasStoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --- alerts ------------------------------------------------------------------


@staff_router.get(
    "/alerts",
    response_model=list[EasCapAlert],
    dependencies=[Depends(require_any_role(*_READ))],
)
def list_alerts(
    active: bool = False,
    source_id: str | None = None,
    store: EasStore | None = Depends(get_eas_store),
) -> list[EasCapAlert]:
    resolved = _require_store(store)
    if active:
        # Manual alerts remain available while feed polling is disabled. Reuse
        # the normal expiry transition so stale alerts leave the active view
        # and remain in history even when the poll worker is not running.
        resolved.expire_alerts()
    return resolved.list_alerts(active_only=active, source_id=source_id)


@staff_router.get(
    "/alerts/{alert_id}",
    response_model=EasCapAlert,
    dependencies=[Depends(require_any_role(*_READ))],
)
def get_alert(alert_id: str, store: EasStore | None = Depends(get_eas_store)) -> EasCapAlert:
    alert = _require_store(store).get_alert(alert_id)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Alert not found.")
    return alert


@staff_router.post(
    "/alerts/manual",
    response_model=EasCapAlert,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_SOURCE_WRITE))],
)
def create_manual_alert(
    payload: ManualAlertInput, request: Request, store: EasStore | None = Depends(get_eas_store)
) -> EasCapAlert:
    operator = _operator_id(request)
    resolved_store = _require_store(store)
    # Provenance guard: a manual alert may only be attributed to a configured source of
    # kind 'manual' — never to a live IPAWS/NWS/AMBER feed's id (which would make a
    # hand-entered alert appear to come from that authoritative feed).
    source = resolved_store.get_source(payload.source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown source_id.")
    if source.kind != "manual":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Manual alerts must use a source of kind 'manual'.",
        )
    now = datetime.now(UTC)
    alert = EasCapAlert(
        alert_id=stable_alert_id(operator, payload.identifier),
        source_id=payload.source_id,
        sender=operator,
        identifier=payload.identifier,
        sent=now,
        msg_type="alert",
        event=payload.event,
        severity=payload.severity,
        headline=payload.headline,
        description=payload.description,
        instruction=payload.instruction,
        areas=payload.areas,
        expires=payload.expires,
    )
    persisted, _ = resolved_store.ingest_alert(alert)
    return persisted


@staff_router.post(
    "/alerts/{alert_id}/display",
    response_model=EasDisplayDecision,
    dependencies=[Depends(require_any_role(*_DISPLAY))],
)
def display_alert(
    alert_id: str,
    payload: DisplayInput,
    request: Request,
    svc: EasDisplayService | None = Depends(get_eas_service),
) -> EasDisplayDecision:
    operator = _operator_id(request)
    from civiccast.eas.presentation import presentation_enabled
    from civiccast.egress.engine_select import gstreamer_engine_selected

    if not presentation_enabled() or not gstreamer_engine_selected():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Broadcast emergency presentation is not enabled. IT must enable CIVICCAST_EAS with the GStreamer engine, then start the channel with its graphics compositor before displaying an alert.",
        )
    daemon = getattr(request.app.state, "egress_daemon", None)
    ready = getattr(daemon, "emergency_presentation_ready", None)
    if not callable(ready) or not ready(payload.channel_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="This channel is not on air with an emergency graphics compositor. Start the configured channel after IT enables CIVICCAST_EAS with GStreamer, then try again.",
        )
    try:
        return _require_service(svc).surface_alert(
            channel_id=payload.channel_id,
            alert_id=alert_id,
            mode=payload.mode,
            decided_by=operator,
            operator_confirmed=payload.operator_confirmed,
        )
    except AlertNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EasDisplayError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# --- display decisions -------------------------------------------------------


@staff_router.get(
    "/decisions",
    response_model=list[EasDisplayDecision],
    dependencies=[Depends(require_any_role(*_READ))],
)
def list_decisions(
    channel_id: str | None = None, store: EasStore | None = Depends(get_eas_store)
) -> list[EasDisplayDecision]:
    return _require_store(store).list_decisions(channel_id=channel_id)


@staff_router.post(
    "/decisions/{decision_id}/clear",
    response_model=EasDisplayDecision,
    dependencies=[Depends(require_any_role(*_DISPLAY))],
)
def clear_decision(
    decision_id: str, svc: EasDisplayService | None = Depends(get_eas_service)
) -> EasDisplayDecision:
    try:
        return _require_service(svc).clear_decision(decision_id)
    except EasStoreError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


__all__ = ["get_eas_service", "get_eas_store", "staff_router"]
