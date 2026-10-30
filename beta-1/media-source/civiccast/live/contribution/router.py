# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S17 Remote Contribution API (build step 9 slice 3c).

Two routers:

* ``/api/staff/contribution`` — gated by the five real roles (``auth/roles.py``)
  via ``require_any_role`` per the S17 §4 table. Room *creation* is a
  ``setup_admin`` (commissioning) act; room list/detail/invite-list reads are
  available to ``setup_admin``, ``meeting_operator`` and ``support_admin``.
  Live-show actions (open/close/invite/admit/on-air/mute/off-air/drop) remain
  ``meeting_operator``; standalone session reads are ``meeting_operator`` or
  ``support_admin``, and diagnostics remain ``support_admin`` only.
* ``/api/public/contribution`` — **token-gated, no auth role.** The opaque
  single-use invite token IS the capability; resolving it consumes it once and
  the guest join page never sees the compositor-facing ``push_url`` (the public
  ``InviteJoinView`` carries only the guest's own ``view_url``).

One DI seam (``get_contribution_service``) returns ``None`` at import so the
module opens no database; the app factory overrides it once durable storage is
ready (one edit, via the shared ``_wire_durable_stores``).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.roles import require_any_role
from civiccast.live.contribution.bridge import VdoBridgeError, VdoDiagnostics
from civiccast.live.contribution.models import (
    CompositorTarget,
    ContributionRole,
    ContributionRoom,
    GuestInvite,
    MediaControlAction,
    RemoteGuestSession,
)
from civiccast.live.contribution.service import (
    ChannelDisabledError,
    ChannelNotConfiguredError,
    ContributionService,
    GuestNotAdmittedError,
    InvalidGuestTransitionError,
    InviteConsumedError,
    InviteExpiredError,
    InviteJoinView,
    RoomClosedError,
    RoomGuestLimitError,
    RoomNotOpenError,
    TakeoverHookError,
)
from civiccast.live.contribution.store import (
    ContributionStoreError,
    GuestSessionNotFoundError,
    InviteNotFoundError,
    RoomNotFoundError,
)

_DB_NOT_READY = "Durable storage is not ready yet."

_ROOM_WRITE = ("setup_admin",)
_ROOM_READ = ("setup_admin", "meeting_operator", "support_admin")
_OPERATE = ("meeting_operator",)
_SESSION_READ = ("meeting_operator", "support_admin")
_DIAG = ("support_admin",)


# --- DI seam (overridden by the app factory) --------------------------------


def get_contribution_service() -> ContributionService | None:
    return None


def _require_service(svc: ContributionService | None) -> ContributionService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


# --- request / response bodies ----------------------------------------------


class CreateRoomInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    name: str
    max_guests: int = 6
    compositor_target: CompositorTarget = "gst_compositor"


class MintInviteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guest_display_name: str
    role: ContributionRole


class MediaControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: MediaControlAction


class RoomOpened(BaseModel):
    """A room that has been opened, plus the operator director-view URL the
    console embeds via the VDO.Ninja IFRAME API."""

    model_config = ConfigDict(extra="forbid")

    room: ContributionRoom
    director_url: str


class RoomDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room: ContributionRoom
    invites: list[GuestInvite]
    sessions: list[RemoteGuestSession]


class AcceptTermsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    terms_version: str


staff_router = APIRouter(prefix="/api/staff/contribution", tags=["staff", "contribution"])
public_router = APIRouter(prefix="/api/public/contribution", tags=["public", "contribution"])


def _translate(exc: ContributionStoreError) -> HTTPException:
    if isinstance(exc, (ChannelDisabledError, ChannelNotConfiguredError)):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if isinstance(exc, (RoomNotFoundError, InviteNotFoundError, GuestSessionNotFoundError)):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (InviteExpiredError, InviteConsumedError)):
        # Single-use / time-limited capability spent — 410 Gone is the honest code.
        return HTTPException(status.HTTP_410_GONE, detail=str(exc))
    if isinstance(
        exc,
        (
            RoomNotOpenError,
            RoomClosedError,
            RoomGuestLimitError,
            GuestNotAdmittedError,
            InvalidGuestTransitionError,
        ),
    ):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))


_TIER_UNAVAILABLE = (
    "Remote contribution is not configured (no self-hosted VDO.Ninja URL). "
    "A compositor + VDO.Ninja + coturn must be commissioned before guests can join."
)


# --- staff: rooms ------------------------------------------------------------


@staff_router.post(
    "/rooms",
    response_model=ContributionRoom,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_ROOM_WRITE))],
)
def create_room(
    payload: CreateRoomInput,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> ContributionRoom:
    try:
        return _require_service(svc).create_room(
            channel_id=payload.channel_id,
            name=payload.name,
            max_guests=payload.max_guests,
            compositor_target=payload.compositor_target,
        )
    except ContributionStoreError as exc:
        raise _translate(exc) from exc


@staff_router.get(
    "/rooms",
    response_model=list[ContributionRoom],
    dependencies=[Depends(require_any_role(*_ROOM_READ))],
)
def list_rooms(
    channel_id: str | None = None,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> list[ContributionRoom]:
    return _require_service(svc).list_rooms(channel_id=channel_id)


@staff_router.get(
    "/rooms/{room_id}",
    response_model=RoomDetail,
    dependencies=[Depends(require_any_role(*_ROOM_READ))],
)
def get_room(
    room_id: str,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> RoomDetail:
    service = _require_service(svc)
    room = service.get_room(room_id)
    if room is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=room_id)
    return RoomDetail(
        room=room,
        invites=service.list_invites_for_room(room_id),
        sessions=service.list_sessions(room_id=room_id),
    )


@staff_router.post(
    "/rooms/{room_id}/open",
    response_model=RoomOpened,
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def open_room(
    room_id: str,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> RoomOpened:
    service = _require_service(svc)
    try:
        room, director_url = service.open_room(room_id)
    except VdoBridgeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_TIER_UNAVAILABLE) from exc
    except ContributionStoreError as exc:
        raise _translate(exc) from exc
    return RoomOpened(room=room, director_url=director_url)


@staff_router.post(
    "/rooms/{room_id}/close",
    response_model=ContributionRoom,
    summary="Close the CivicCast room record (does not disconnect provider guests)",
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def close_room(
    room_id: str,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> ContributionRoom:
    try:
        return _require_service(svc).close_room(room_id)
    except ContributionStoreError as exc:
        raise _translate(exc) from exc


# --- staff: invites ----------------------------------------------------------


@staff_router.post(
    "/rooms/{room_id}/invites",
    response_model=GuestInvite,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def mint_invite(
    room_id: str,
    payload: MintInviteInput,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> GuestInvite:
    service = _require_service(svc)
    try:
        return service.mint_invite(
            room_id=room_id,
            guest_display_name=payload.guest_display_name,
            role=payload.role,
        )
    except VdoBridgeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_TIER_UNAVAILABLE) from exc
    except ContributionStoreError as exc:
        raise _translate(exc) from exc


@staff_router.get(
    "/rooms/{room_id}/invites",
    response_model=list[GuestInvite],
    dependencies=[Depends(require_any_role(*_ROOM_READ))],
)
def list_invites(
    room_id: str,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> list[GuestInvite]:
    return _require_service(svc).list_invites_for_room(room_id)


# --- staff: guest sessions ---------------------------------------------------


def _guest_action(
    svc: ContributionService | None, session_id: str, action: str
) -> RemoteGuestSession:
    service = _require_service(svc)
    method = {
        "admit": service.admit_guest,
        "on-air": service.put_on_air,
        "mute": service.mute_guest,
        "off-air": service.take_off_air,
        "drop": service.drop_guest,
    }[action]
    try:
        return method(session_id)
    except TakeoverHookError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ContributionStoreError as exc:
        raise _translate(exc) from exc


@staff_router.post(
    "/sessions/{session_id}/admit",
    response_model=RemoteGuestSession,
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def admit_guest(
    session_id: str, svc: ContributionService | None = Depends(get_contribution_service)
) -> RemoteGuestSession:
    return _guest_action(svc, session_id, "admit")


@staff_router.post(
    "/sessions/{session_id}/on-air",
    response_model=RemoteGuestSession,
    summary="Route an admitted guest into the live channel and confirm the handoff",
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def put_on_air(
    session_id: str, svc: ContributionService | None = Depends(get_contribution_service)
) -> RemoteGuestSession:
    return _guest_action(svc, session_id, "on-air")


@staff_router.post(
    "/sessions/{session_id}/mute",
    response_model=RemoteGuestSession,
    summary="Mute the guest in CivicCast's live audio mix",
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def mute_guest(
    session_id: str, svc: ContributionService | None = Depends(get_contribution_service)
) -> RemoteGuestSession:
    return _guest_action(svc, session_id, "mute")


@staff_router.post(
    "/sessions/{session_id}/off-air",
    response_model=RemoteGuestSession,
    summary="Remove the guest from the live mix and return to scheduled output if last",
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def off_air_guest(
    session_id: str, svc: ContributionService | None = Depends(get_contribution_service)
) -> RemoteGuestSession:
    return _guest_action(svc, session_id, "off-air")


@staff_router.post(
    "/sessions/{session_id}/drop",
    response_model=RemoteGuestSession,
    summary="Drop the guest and remove them from the live mix",
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def drop_guest(
    session_id: str, svc: ContributionService | None = Depends(get_contribution_service)
) -> RemoteGuestSession:
    return _guest_action(svc, session_id, "drop")


@staff_router.post(
    "/sessions/{session_id}/media-control-request",
    response_model=RemoteGuestSession,
    dependencies=[Depends(require_any_role(*_OPERATE))],
)
def record_media_control_request(
    session_id: str,
    body: MediaControlRequest,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> RemoteGuestSession:
    """Record a browser-sent provider command as unverified, never completed."""
    try:
        return _require_service(svc).record_media_control_request(session_id, body.action)
    except ContributionStoreError as exc:
        raise _translate(exc) from exc


@staff_router.get(
    "/sessions",
    response_model=list[RemoteGuestSession],
    dependencies=[Depends(require_any_role(*_SESSION_READ))],
)
def list_sessions(
    room_id: str | None = None,
    active_only: bool = False,
    svc: ContributionService | None = Depends(get_contribution_service),
) -> list[RemoteGuestSession]:
    return _require_service(svc).list_sessions(room_id=room_id, active_only=active_only)


@staff_router.get(
    "/diagnostics", response_model=VdoDiagnostics, dependencies=[Depends(require_any_role(*_DIAG))]
)
def diagnostics(
    svc: ContributionService | None = Depends(get_contribution_service),
) -> VdoDiagnostics:
    return _require_service(svc).diagnostics()


@staff_router.post(
    "/diagnostics/turn-test",
    response_model=VdoDiagnostics,
    summary="Probe TURN reachability right now (does not wait for the next background poll)",
    dependencies=[Depends(require_any_role(*_DIAG))],
)
def test_turn_connectivity(
    svc: ContributionService | None = Depends(get_contribution_service),
) -> VdoDiagnostics:
    """The "Test TURN connectivity" button in the contribution setup UI.

    Covers both postures: a locally-supervised coturn (Linux/macOS) and the
    owner-approved documented-external-TURN posture (coturn has no native
    Windows build — see civiccast/installer/contribution_install.py). Either
    way this probes the effective CIVICCAST_TURN_HOST/CIVICCAST_TURN_PORT
    right now and returns refreshed diagnostics, never a stale cached result.
    """
    return _require_service(svc).test_turn_connectivity()


# --- public: token-gated join + terms ---------------------------------------


@public_router.get("/invites/{invite_token}", response_model=InviteJoinView)
def resolve_invite(
    invite_token: Annotated[str, Path(min_length=32, max_length=200)],
    svc: ContributionService | None = Depends(get_contribution_service),
) -> InviteJoinView:
    """Resolve a single-use invite. Consumes the token + creates a HELD guest
    session once the join can proceed; returns a needs-terms view (token NOT
    consumed) when terms are still required. No auth — the token is the
    capability. Never returns the compositor-facing push_url."""
    try:
        return _require_service(svc).resolve_invite(invite_token)
    except ContributionStoreError as exc:
        raise _translate(exc) from exc


@public_router.post("/invites/{invite_token}/accept-terms", response_model=AcceptTermsResponse)
def accept_terms(
    invite_token: Annotated[str, Path(min_length=32, max_length=200)],
    svc: ContributionService | None = Depends(get_contribution_service),
) -> AcceptTermsResponse:
    try:
        invite = _require_service(svc).accept_terms(invite_token)
    except ContributionStoreError as exc:
        raise _translate(exc) from exc
    return AcceptTermsResponse(accepted=True, terms_version=invite.terms_version or "")


__all__ = [
    "get_contribution_service",
    "public_router",
    "staff_router",
]
