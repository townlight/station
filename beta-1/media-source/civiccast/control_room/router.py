# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S16 Production & Control Room staff API (build step 9 slice 2c).

All endpoints under ``/api/staff/control-room``, gated by the five real roles
(``auth/roles.py``) via ``require_any_role`` per the S16 §4 table. Device
*configuration* is a ``setup_admin`` (commissioning) act; live *operation*
(open session, plan, fire) is ``meeting_operator``; read-only diagnostics add
``support_admin``. The operator id on session/fire actions comes from the
VERIFIED token identity (``request.state.operator_identity``), never the body.

DI seams (``get_*``) return ``None`` at import so the module opens no database;
the app factory overrides them once durable storage is ready (one edit, via the
shared ``_wire_durable_stores``). Device secrets are write-only: a ``secret`` on
the device input is ``exclude=True`` (never serialized back) and is persisted to
the keyring under an opaque handle by the injected secret writer.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import require_any_role
from civiccast.control_room.models import (
    ControlRoomReadinessReport,
    ControlRoomSession,
    ControlSurface,
    CueFiredEvent,
    CuePlan,
    DeviceProfile,
    DeviceTransport,
    ProductionDevice,
    ProductionDeviceKind,
    SessionMode,
    SurfaceRole,
    TimelineCue,
)
from civiccast.control_room.policy import ControlRoomPolicyError, validate_cue_for_device
from civiccast.control_room.service import (
    ControlRoomService,
    ControlRoomServiceError,
    CueMaterialStateChangedError,
    CueNotReadyError,
    CuePolicyError,
    CueSurfaceMismatchError,
    OnAirConfirmationRequiredError,
    OnAirReadinessBlockedError,
    OnAirSessionExpiredError,
    RollbackNotAvailableError,
    SafeStateCueRequiredError,
    SessionAlreadyOpenError,
    SessionClosedError,
    SessionLockOverrideForbiddenError,
    SessionOperatorMismatchError,
)
from civiccast.control_room.store import (
    ControlRoomStore,
    ControlRoomStoreError,
    CueImmutableError,
    CueNotFoundError,
    DeviceNotFoundError,
    SessionNotFoundError,
    SurfaceNotFoundError,
)
from civiccast.control_room.tsr_client import TsrClientError, TsrProbeResult

# Persists a device control secret under an opaque handle (keyring-backed).
DeviceSecretWriter = Callable[[str, str], None]

_DB_NOT_READY = "Durable storage is not ready yet."

_DEVICE_READ = ("setup_admin", "support_admin", "meeting_operator")
_DEVICE_WRITE = ("setup_admin",)
_PROBE = ("setup_admin", "support_admin")
_SURFACE_READ = ("meeting_operator", "support_admin", "setup_admin")
_SURFACE_WRITE = ("setup_admin",)
_SESSION_OP = ("meeting_operator",)
_SESSION_CLOSE = ("meeting_operator", "setup_admin", "support_admin")
_LOCK_OVERRIDE_ROLES = frozenset({"setup_admin", "support_admin"})
_SESSION_READ = ("meeting_operator", "support_admin")
_PLAN = ("meeting_operator", "support_admin")
_FIRE = ("meeting_operator",)
_ROLLBACK = ("meeting_operator",)
_READINESS = ("setup_admin", "support_admin", "meeting_operator")
_READINESS_EXTRA = {"x-required-roles": list(_READINESS)}


# --- DI seams (overridden by the app factory) -------------------------------


def get_control_room_store() -> ControlRoomStore | None:
    return None


def get_control_room_service() -> ControlRoomService | None:
    return None


def get_device_secret_writer() -> DeviceSecretWriter | None:
    return None


def _require_store(store: ControlRoomStore | None) -> ControlRoomStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_service(svc: ControlRoomService | None) -> ControlRoomService:
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


def _now() -> datetime:
    return datetime.now(UTC)


# --- request bodies ----------------------------------------------------------


class ProductionDeviceInput(BaseModel):
    """Create/update a device. ``secret`` is write-only — it goes to the keyring
    under an opaque handle and is never serialized back."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=160)]
    kind: ProductionDeviceKind
    transport: DeviceTransport
    host: Annotated[str | None, Field(default=None, max_length=255)] = None
    port: Annotated[int | None, Field(default=None, ge=1, le=65535)] = None
    enabled: bool = True
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    secret: Annotated[str | None, Field(default=None, exclude=True, max_length=2000)] = None


class DeviceProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tsr_device_type: Annotated[str, Field(min_length=1, max_length=60)]
    options: dict[str, Any] = Field(default_factory=dict)
    capability_map: dict[str, Any] = Field(default_factory=dict)
    take_delay_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0
    post_roll_ms: Annotated[int, Field(default=0, ge=0, le=600000)] = 0


class ControlSurfaceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=160)]
    assigned_role: SurfaceRole = "meeting_operator"


class TimelineCueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=160)]
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    action: Annotated[str, Field(min_length=1, max_length=30)]
    payload: dict[str, Any] = Field(default_factory=dict)
    confirm_required: bool = False
    bank: Annotated[int, Field(default=0, ge=0, le=99)] = 0
    position: Annotated[int, Field(default=0, ge=0, le=999)] = 0


class SessionOpenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface_id: Annotated[str, Field(min_length=1, max_length=120)]
    program_feed_source_ref: Annotated[str | None, Field(default=None, max_length=120)] = None
    mode: SessionMode = "test"
    safe_state_cue_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    confirm_on_air: bool = False


class FireCueInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_state_fingerprint: Annotated[
        str | None, Field(default=None, min_length=1, max_length=128)
    ] = None


class SurfaceDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: ControlSurface
    cues: list[TimelineCue]


staff_router = APIRouter(prefix="/api/staff/control-room", tags=["staff", "control-room"])


def _translate(exc: ControlRoomServiceError | ControlRoomStoreError) -> HTTPException:
    if isinstance(
        exc, (DeviceNotFoundError, SurfaceNotFoundError, CueNotFoundError, SessionNotFoundError)
    ):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, SessionAlreadyOpenError):
        holder = exc.existing_session.operator_name or exc.existing_session.operator_id
        return HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"A session is already open on this surface, locked by {holder} since "
                f"{exc.existing_session.started_at.isoformat()}. A setup admin or support "
                "admin can force-close it to release the lock."
            ),
        )
    if isinstance(exc, (SessionLockOverrideForbiddenError, SessionOperatorMismatchError)):
        return HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, CueImmutableError):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(
        exc,
        (
            SessionClosedError,
            CueSurfaceMismatchError,
            CueNotReadyError,
            CuePolicyError,
            OnAirConfirmationRequiredError,
            OnAirReadinessBlockedError,
            OnAirSessionExpiredError,
            SafeStateCueRequiredError,
            RollbackNotAvailableError,
        ),
    ):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, CueMaterialStateChangedError):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


# --- devices -----------------------------------------------------------------


@staff_router.get(
    "/devices",
    response_model=list[ProductionDevice],
    dependencies=[Depends(require_any_role(*_DEVICE_READ))],
)
def list_devices(
    store: ControlRoomStore | None = Depends(get_control_room_store),
) -> list[ProductionDevice]:
    return _require_store(store).list_devices()


@staff_router.post(
    "/devices",
    response_model=ProductionDevice,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_DEVICE_WRITE))],
)
def create_device(
    payload: ProductionDeviceInput,
    store: ControlRoomStore | None = Depends(get_control_room_store),
    secret_writer: DeviceSecretWriter | None = Depends(get_device_secret_writer),
) -> ProductionDevice:
    st = _require_store(store)
    device_id = f"pdev_{uuid.uuid4().hex}"
    secret_ref = _persist_secret(secret_writer, device_id, payload.secret)
    now = _now()
    return st.upsert_device(
        ProductionDevice(
            device_id=device_id,
            label=payload.label,
            kind=payload.kind,
            transport=payload.transport,
            host=payload.host,
            port=payload.port,
            enabled=payload.enabled,
            notes=payload.notes,
            secret_ref=secret_ref,
            created_at=now,
            updated_at=now,
        )
    )


@staff_router.patch(
    "/devices/{device_id}",
    response_model=ProductionDevice,
    dependencies=[Depends(require_any_role(*_DEVICE_WRITE))],
)
def update_device(
    device_id: str,
    payload: ProductionDeviceInput,
    store: ControlRoomStore | None = Depends(get_control_room_store),
    secret_writer: DeviceSecretWriter | None = Depends(get_device_secret_writer),
) -> ProductionDevice:
    st = _require_store(store)
    existing = st.get_device(device_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=device_id)
    # A new secret rotates the keyring entry; otherwise keep the existing handle.
    secret_ref = (
        _persist_secret(secret_writer, device_id, payload.secret)
        if payload.secret is not None
        else existing.secret_ref
    )
    return st.upsert_device(
        ProductionDevice(
            device_id=device_id,
            label=payload.label,
            kind=payload.kind,
            transport=payload.transport,
            host=payload.host,
            port=payload.port,
            enabled=payload.enabled,
            notes=payload.notes,
            secret_ref=secret_ref,
            created_at=existing.created_at,
            updated_at=_now(),
        )
    )


@staff_router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_DEVICE_WRITE))],
)
def delete_device(
    device_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> None:
    try:
        _require_store(store).delete_device(device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=device_id) from exc


@staff_router.get(
    "/devices/{device_id}/profile",
    response_model=DeviceProfile | None,
    dependencies=[Depends(require_any_role(*_DEVICE_READ))],
)
def get_profile(
    device_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> DeviceProfile | None:
    st = _require_store(store)
    if st.get_device(device_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=device_id)
    return st.get_profile_for_device(device_id)


@staff_router.put(
    "/devices/{device_id}/profile",
    response_model=DeviceProfile,
    dependencies=[Depends(require_any_role(*_DEVICE_WRITE))],
)
def upsert_profile(
    device_id: str,
    payload: DeviceProfileInput,
    store: ControlRoomStore | None = Depends(get_control_room_store),
) -> DeviceProfile:
    st = _require_store(store)
    if st.get_device(device_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=device_id)
    prev = st.get_profile_for_device(device_id)
    now = _now()
    return st.upsert_profile(
        DeviceProfile(
            profile_id=f"pprof_{device_id}",
            device_id=device_id,
            tsr_device_type=payload.tsr_device_type,
            options=payload.options,
            capability_map=payload.capability_map,
            take_delay_ms=payload.take_delay_ms,
            post_roll_ms=payload.post_roll_ms,
            version=(prev.version + 1) if prev is not None else 1,
            created_at=now,
            updated_at=now,
        )
    )


@staff_router.post(
    "/devices/{device_id}/probe",
    response_model=TsrProbeResult,
    dependencies=[Depends(require_any_role(*_PROBE))],
)
def probe_device(
    device_id: str,
    svc: ControlRoomService | None = Depends(get_control_room_service),
) -> TsrProbeResult:
    try:
        return _require_service(svc).probe_device(device_id=device_id)
    except DeviceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=device_id) from exc
    except (ControlRoomServiceError, ControlRoomStoreError) as exc:
        raise _translate(exc) from exc


def _persist_secret(
    writer: DeviceSecretWriter | None, device_id: str, secret: str | None
) -> str | None:
    """Store ``secret`` (if any) under an opaque handle and return the handle.
    Never returns or logs the secret itself."""
    if secret is None:
        return None
    if writer is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The credential store is not available to persist the device secret.",
        )
    handle = f"crsecret_{device_id}"
    writer(handle, secret)
    return handle


# --- surfaces + cues ---------------------------------------------------------


@staff_router.get(
    "/surfaces",
    response_model=list[ControlSurface],
    dependencies=[Depends(require_any_role(*_SURFACE_READ))],
)
def list_surfaces(
    store: ControlRoomStore | None = Depends(get_control_room_store),
) -> list[ControlSurface]:
    return _require_store(store).list_surfaces()


@staff_router.get(
    "/surfaces/{surface_id}",
    response_model=SurfaceDetail,
    dependencies=[Depends(require_any_role(*_SURFACE_READ))],
)
def get_surface(
    surface_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> SurfaceDetail:
    st = _require_store(store)
    surface = st.get_surface(surface_id)
    if surface is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=surface_id)
    return SurfaceDetail(surface=surface, cues=st.list_cues_for_surface(surface_id))


@staff_router.get(
    "/surfaces/{surface_id}/session",
    response_model=ControlRoomSession | None,
    summary="Read the open session and lock holder for a control surface",
    dependencies=[Depends(require_any_role(*_SESSION_CLOSE))],
)
def get_open_surface_session(
    surface_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> ControlRoomSession | None:
    st = _require_store(store)
    if st.get_surface(surface_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=surface_id)
    return st.get_open_session_for_surface(surface_id)


@staff_router.post(
    "/surfaces",
    response_model=ControlSurface,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_SURFACE_WRITE))],
)
def create_surface(
    payload: ControlSurfaceInput,
    request: Request,
    store: ControlRoomStore | None = Depends(get_control_room_store),
) -> ControlSurface:
    now = _now()
    return _require_store(store).upsert_surface(
        ControlSurface(
            surface_id=f"crsrf_{uuid.uuid4().hex}",
            label=payload.label,
            assigned_role=payload.assigned_role,
            created_by=_operator_id(request),
            created_at=now,
            updated_at=now,
        )
    )


@staff_router.patch(
    "/surfaces/{surface_id}",
    response_model=ControlSurface,
    dependencies=[Depends(require_any_role(*_SURFACE_WRITE))],
)
def update_surface(
    surface_id: str,
    payload: ControlSurfaceInput,
    store: ControlRoomStore | None = Depends(get_control_room_store),
) -> ControlSurface:
    st = _require_store(store)
    existing = st.get_surface(surface_id)
    if existing is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=surface_id)
    return st.upsert_surface(
        ControlSurface(
            surface_id=surface_id,
            label=payload.label,
            assigned_role=payload.assigned_role,
            created_by=existing.created_by,
            created_at=existing.created_at,
            updated_at=_now(),
        )
    )


@staff_router.delete(
    "/surfaces/{surface_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_SURFACE_WRITE))],
)
def delete_surface(
    surface_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> None:
    try:
        _require_store(store).delete_surface(surface_id)
    except SurfaceNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=surface_id) from exc


@staff_router.post(
    "/surfaces/{surface_id}/cues",
    response_model=TimelineCue,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_SURFACE_WRITE))],
)
def create_cue(
    surface_id: str,
    payload: TimelineCueInput,
    store: ControlRoomStore | None = Depends(get_control_room_store),
) -> TimelineCue:
    st = _require_store(store)
    if st.get_surface(surface_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=surface_id)
    device = st.get_device(payload.device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=payload.device_id)
    try:
        cue = TimelineCue(
            cue_id=f"crcue_{uuid.uuid4().hex}",
            surface_id=surface_id,
            label=payload.label,
            device_id=payload.device_id,
            action=payload.action,  # type: ignore[arg-type]
            payload=payload.payload,
            confirm_required=payload.confirm_required,
            bank=payload.bank,
            position=payload.position,
            proof_boundary="Cue plan preview only; no device socket is opened by this API.",
            created_at=_now(),
        )
        validate_cue_for_device(device, cue)
        return st.upsert_cue(cue)
    except (ValueError, ControlRoomPolicyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@staff_router.delete(
    "/cues/{cue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_SURFACE_WRITE))],
)
def delete_cue(
    cue_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> None:
    try:
        _require_store(store).delete_cue(cue_id)
    except CueNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=cue_id) from exc
    except CueImmutableError as exc:
        raise _translate(exc) from exc


# --- sessions + plan/fire ----------------------------------------------------


@staff_router.get(
    "/readiness",
    response_model=ControlRoomReadinessReport,
    summary="Read control-room readiness (local configuration only; not station-device evidence)",
    description=(
        "Returns CivicCast's local production-control readiness report. This endpoint is "
        "configuration and policy readiness only: it is not clean Windows install evidence, "
        "not simulator evidence, not real OBS/vMix/ATEM/NDI evidence, and not "
        "station-device evidence."
    ),
    responses={
        403: {
            "description": "Staff identity lacks setup_admin, support_admin, or meeting_operator."
        },
        503: {"description": _DB_NOT_READY},
    },
    openapi_extra=_READINESS_EXTRA,
    dependencies=[Depends(require_any_role(*_READINESS))],
)
def readiness_report(
    svc: ControlRoomService | None = Depends(get_control_room_service),
) -> ControlRoomReadinessReport:
    return _require_service(svc).readiness_report()


@staff_router.post(
    "/sessions",
    response_model=ControlRoomSession,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_SESSION_OP))],
)
def open_session(
    payload: SessionOpenInput,
    request: Request,
    svc: ControlRoomService | None = Depends(get_control_room_service),
) -> ControlRoomSession:
    identity = getattr(request.state, "operator_identity", None)
    name = identity.operator_display_name if isinstance(identity, OperatorIdentity) else None
    try:
        return _require_service(svc).open_session(
            surface_id=payload.surface_id,
            operator_id=_operator_id(request),
            operator_name=name,
            program_feed_source_ref=payload.program_feed_source_ref,
            mode=payload.mode,
            safe_state_cue_id=payload.safe_state_cue_id,
            confirm_on_air=payload.confirm_on_air,
        )
    except (ControlRoomServiceError, ControlRoomStoreError) as exc:
        raise _translate(exc) from exc


@staff_router.get(
    "/sessions/{session_id}",
    response_model=ControlRoomSession,
    dependencies=[Depends(require_any_role(*_SESSION_READ))],
)
def get_session(
    session_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> ControlRoomSession:
    found = _require_store(store).get_session(session_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=session_id)
    return found


@staff_router.get(
    "/sessions/{session_id}/audit",
    response_model=list[CueFiredEvent],
    dependencies=[Depends(require_any_role(*_SESSION_READ))],
)
def session_audit(
    session_id: str, store: ControlRoomStore | None = Depends(get_control_room_store)
) -> list[CueFiredEvent]:
    return _require_store(store).list_cue_events(session_id)


@staff_router.delete(
    "/sessions/{session_id}",
    response_model=ControlRoomSession,
    summary="Close a session, releasing its operator lock on the surface",
    description=(
        "The owning operator can always close their own session. A setup admin or support "
        "admin may force-close ANY open session to release a stuck/abandoned operator lock."
    ),
    dependencies=[Depends(require_any_role(*_SESSION_CLOSE))],
)
def close_session(
    session_id: str,
    request: Request,
    svc: ControlRoomService | None = Depends(get_control_room_service),
) -> ControlRoomSession:
    identity = getattr(request.state, "operator_identity", None)
    scopes = identity.scopes if isinstance(identity, OperatorIdentity) else ()
    is_override = any(role in _LOCK_OVERRIDE_ROLES for role in scopes)
    try:
        return _require_service(svc).close_session(
            session_id=session_id,
            requested_by=_operator_id(request),
            is_lock_override=is_override,
        )
    except (ControlRoomServiceError, ControlRoomStoreError) as exc:
        raise _translate(exc) from exc


@staff_router.post(
    "/sessions/{session_id}/rollback",
    response_model=CueFiredEvent,
    summary="Fire the session's configured safe-state/panic cue",
    description=(
        "Partial-failure recovery policy: returns the surface to its known-safe cue. Not a "
        "generic undo of a specific cue — production-control actions have no automatic inverse."
    ),
    dependencies=[Depends(require_any_role(*_ROLLBACK))],
)
def rollback_session(
    session_id: str,
    request: Request,
    svc: ControlRoomService | None = Depends(get_control_room_service),
) -> CueFiredEvent:
    service = _require_service(svc)
    try:
        return service.rollback_session(session_id=session_id, operator_id=_operator_id(request))
    except TsrClientError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"Production control service error ({type(exc).__name__}).",
        ) from exc
    except (ControlRoomServiceError, ControlRoomStoreError) as exc:
        raise _translate(exc) from exc


@staff_router.post(
    "/sessions/{session_id}/cues/{cue_id}/plan",
    response_model=CuePlan,
    dependencies=[Depends(require_any_role(*_PLAN))],
)
def plan_cue(
    session_id: str, cue_id: str, svc: ControlRoomService | None = Depends(get_control_room_service)
) -> CuePlan:
    try:
        return _require_service(svc).plan_cue(session_id=session_id, cue_id=cue_id)
    except (ControlRoomServiceError, ControlRoomStoreError) as exc:
        raise _translate(exc) from exc


@staff_router.post(
    "/sessions/{session_id}/cues/{cue_id}/fire",
    response_model=CueFiredEvent,
    dependencies=[Depends(require_any_role(*_FIRE))],
)
def fire_cue(
    session_id: str,
    cue_id: str,
    request: Request,
    payload: FireCueInput | None = None,
    svc: ControlRoomService | None = Depends(get_control_room_service),
) -> CueFiredEvent:
    service = _require_service(svc)
    try:
        return service.fire_cue(
            session_id=session_id,
            cue_id=cue_id,
            operator_id=_operator_id(request),
            expected_material_state_fingerprint=(
                payload.material_state_fingerprint if payload is not None else None
            ),
        )
    except TsrClientError as exc:
        # The cue attempt is already audited (result=failed) before this raises;
        # report only the failure type, never device secrets/payloads.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"Production control service error ({type(exc).__name__}).",
        ) from exc
    except (ControlRoomServiceError, ControlRoomStoreError) as exc:
        raise _translate(exc) from exc


__all__ = [
    "get_control_room_service",
    "get_control_room_store",
    "get_device_secret_writer",
    "staff_router",
]
