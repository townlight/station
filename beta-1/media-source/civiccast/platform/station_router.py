# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI routes for S1 StationBoxProfile and the station identity profile.

Two distinct concerns, deliberately not conflated (S1 §2 "identity and
capability are deliberately separate concerns"):

* ``GET /api/staff/station-box-profile`` / ``/readiness`` — the S1
  ``StationBoxProfile`` capability report (computed, read-only).
* ``GET``/``PUT /api/staff/station/profile`` — the mutable operator
  identity profile (station name, timezone, storage roots, default
  channel) persisted in station-state, with the env-override precedence
  loader in ``civiccast.installer.station_state``.

Deviation from the S1 spec's literal path (documented, deliberate): S1 §4
writes the box-profile paths as ``/api/station-box-profile`` (no
``/api/staff`` prefix), by analogy with the existing public
``/api/hardware`` endpoint. That analogy does not hold here: the S1 spec
gates these routes to specific staff roles via ``require_any_role``, but
this codebase's staff bearer-token auth middleware
(``civiccast.auth.middleware.staff_auth_middleware``) only authenticates
requests whose path starts with ``/api/staff/`` — it is what populates
``request.state.operator_identity`` in the first place. A role-gated route
outside that prefix would never see an identity and would 401 for every
caller, including legitimately-scoped staff, which defeats the spec's
intent of "read-only diagnostic surfaces available to setup_admin /
meeting_operator / support_admin." Mounting under ``/api/staff/`` is the
functionally-correct choice; ``/api/hardware`` stays unprefixed because it
is genuinely public (no role gate at all).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from civiccast.auth.roles import require_any_role
from civiccast.installer.models import DeploymentProfile, StationProfile
from civiccast.installer.service import build_backup_status
from civiccast.installer.station_state import (
    StationProfileUpdateRequest,
    StationSetupNotCompleteError,
    resolve_live_captions_enabled,
    resolve_station_display_name,
    resolve_station_storage_locations,
    resolve_station_timezone,
    update_station_profile_fields,
)
from civiccast.platform.station_box_profile import (
    PegReadinessRollup,
    StationBoxProfile,
    probe_station_box_profile,
)
from civiccast.programlog.router import get_program_log_materializer

_DIAGNOSTIC_ROLES = ("setup_admin", "meeting_operator", "support_admin")
_SCHEDULING_TIMEZONE_READ_ROLES = (*_DIAGNOSTIC_ROLES, "publish_operator")


class StationTimeZone(BaseModel):
    """The minimal station setting required to enter scheduling times."""

    model_config = ConfigDict(extra="forbid")

    station_timezone: str


box_profile_router = APIRouter(prefix="/api/staff", tags=["staff", "platform"])

staff_router = APIRouter(prefix="/api/staff/station", tags=["staff", "station"])


@box_profile_router.get(
    "/station-box-profile",
    response_model=StationBoxProfile,
    summary="Full StationBoxProfile: cable/PEG appliance-readiness report",
    dependencies=[Depends(require_any_role(*_DIAGNOSTIC_ROLES))],
)
def get_station_box_profile(
    deployment_profile: DeploymentProfile = "public-meetings",
) -> StationBoxProfile:
    return probe_station_box_profile(
        deployment_profile=deployment_profile, backup_status=build_backup_status()
    )


@box_profile_router.get(
    "/station-box-profile/readiness",
    response_model=PegReadinessRollup,
    summary="PEG readiness roll-up only (cheap poll target for S8 alerting)",
    dependencies=[Depends(require_any_role(*_DIAGNOSTIC_ROLES))],
)
def get_station_box_profile_readiness(
    deployment_profile: DeploymentProfile = "public-meetings",
) -> PegReadinessRollup:
    profile = probe_station_box_profile(
        deployment_profile=deployment_profile, backup_status=build_backup_status()
    )
    return profile.peg_readiness


@staff_router.get(
    "/profile",
    response_model=StationProfile,
    summary="Read the station identity profile (name, timezone, storage roots, channel)",
    dependencies=[Depends(require_any_role(*_DIAGNOSTIC_ROLES))],
)
def get_station_profile() -> StationProfile:
    from civiccast.installer.service import operator_console_url
    from civiccast.installer.station_state import read_station_setup_state

    state = read_station_setup_state(operator_console_url=operator_console_url())
    if state.profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="First-admin setup has not completed yet; no station profile exists.",
        )
    # Report the resolved (env-override-aware) values for the fields that
    # have a loader, so the console shows what is actually in effect —
    # never the persisted-only value when an env override is active.
    return state.profile.model_copy(
        update={
            "station_name": resolve_station_display_name(),
            "station_timezone": resolve_station_timezone(),
            "storage_locations": resolve_station_storage_locations(),
            # Same rule: report what is IN EFFECT. `CIVICCAST_CAPTION_TAP=off`
            # in the environment forces live captioning off regardless of what
            # is persisted, and the console must not show "on" while the tap
            # is not running.
            "live_captions_enabled": resolve_live_captions_enabled(),
        }
    )


@staff_router.get(
    "/timezone",
    response_model=StationTimeZone,
    summary="Read the station timezone used by scheduling forms",
    dependencies=[Depends(require_any_role(*_SCHEDULING_TIMEZONE_READ_ROLES))],
)
def get_station_timezone() -> StationTimeZone:
    from civiccast.installer.service import operator_console_url
    from civiccast.installer.station_state import read_station_setup_state

    state = read_station_setup_state(operator_console_url=operator_console_url())
    if state.profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="First-admin setup has not completed yet; no station profile exists.",
        )
    return StationTimeZone(station_timezone=resolve_station_timezone())


@staff_router.put(
    "/profile",
    response_model=StationProfile,
    summary=(
        "Edit the mutable station identity profile fields (including the live-caption switch)"
    ),
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def put_station_profile(
    payload: StationProfileUpdateRequest,
    program_log_materializer: Any = Depends(get_program_log_materializer),
) -> StationProfile:
    try:
        if payload.station_timezone is not None and program_log_materializer is not None:
            from civiccast.installer.service import operator_console_url
            from civiccast.installer.station_state import read_station_setup_state

            state = read_station_setup_state(operator_console_url=operator_console_url())
            if (
                state.profile is not None
                and payload.station_timezone.strip() != state.profile.station_timezone
            ):
                # Pin the current station-local intent before the dynamic TZ
                # provider starts resolving to the new profile value.
                program_log_materializer.pin_legacy_anchors()
        return update_station_profile_fields(payload)
    except StationSetupNotCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
