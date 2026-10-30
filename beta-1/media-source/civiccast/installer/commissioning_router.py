# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI routes for the S3 commissioning wizard (screens 8-11).

Write/orchestration endpoints require ``setup_admin`` (S3 §4: "commissioning
is a setup activity"); the read-only checks/state endpoints additionally
accept ``support_admin`` (diagnostic role). Mirrors the
``civiccast.egress.router`` dependency-injection pattern for the egress
store so tests can override it the same way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.roles import require_any_role
from civiccast.egress.store import EgressStore
from civiccast.installer.commissioning import (
    ChannelCommissioningSetup,
    ChannelSetupValidationError,
    CommissioningCheckReport,
    CommissioningProofRun,
    CommissioningReport,
    CommissioningState,
    OutputProofSettings,
    build_commissioning_report,
    run_first_run_cable_checks,
    run_output_proof,
    validate_channel_commissioning_setup,
)
from civiccast.installer.models import DeploymentProfile
from civiccast.installer.station_state import (
    read_commissioning_state,
    save_channel_commissioning_setup,
    save_commissioning_checks,
    save_commissioning_proof_run,
    save_commissioning_report,
)

_WRITE_ROLES = ("setup_admin",)
_READ_ROLES = ("setup_admin", "support_admin")

staff_router = APIRouter(prefix="/api/staff/cable/commissioning", tags=["staff", "commissioning"])


def get_commissioning_egress_store() -> EgressStore | None:
    """FastAPI dependency for the active egress store (overridden by the app factory)."""


class CommissioningChecksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_profile: DeploymentProfile = "public-meetings"
    station_name: Annotated[str, Field(max_length=120)] = ""


@staff_router.get(
    "/state",
    response_model=CommissioningState,
    summary="Resumable commissioning progress across all 4 steps",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
)
def get_commissioning_state() -> CommissioningState:
    return read_commissioning_state()


@staff_router.post(
    "/checks",
    response_model=CommissioningCheckReport,
    summary="S3 Screen 8: run the first-run cable commissioning checks",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES, *_READ_ROLES))],
)
def post_commissioning_checks(payload: CommissioningChecksRequest) -> CommissioningCheckReport:
    report = run_first_run_cable_checks(
        deployment_profile=payload.deployment_profile, station_name=payload.station_name
    )
    save_commissioning_checks(report)
    return report


@staff_router.post(
    "/channel-setup",
    response_model=ChannelCommissioningSetup,
    summary="S3 Screen 9: validate and persist the channel output setup",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={422: {"description": "Channel setup failed validation."}},
)
def post_channel_setup(payload: ChannelCommissioningSetup) -> ChannelCommissioningSetup:
    try:
        setup = validate_channel_commissioning_setup(payload)
    except ChannelSetupValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    save_channel_commissioning_setup(setup)
    return setup


@staff_router.post(
    "/output-proof",
    response_model=CommissioningProofRun,
    summary="S3 Screen 10: run the bounded output-proof test pattern + TSDuck probe",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Egress config not found for the channel."},
        503: {"description": "Durable storage is not ready."},
    },
)
def post_output_proof(
    payload: OutputProofSettings,
    egress_store: EgressStore | None = Depends(get_commissioning_egress_store),
) -> CommissioningProofRun:
    if egress_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Durable storage is not ready; the output proof needs the channel's egress config.",
        )
    config = egress_store.get_config(payload.channel_id)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No egress config for channel {payload.channel_id!r}.",
        )
    state = read_commissioning_state()
    cea708_expected = bool(state.channel_setup and state.channel_setup.cea708_passthrough)
    run = run_output_proof(payload, config=config, cea708_expected=cea708_expected)
    save_commissioning_proof_run(run)
    return run


@staff_router.post(
    "/report",
    response_model=CommissioningReport,
    summary="S3 Screen 11: build and persist the final commissioning report",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={409: {"description": "An earlier commissioning step has not completed yet."}},
)
def post_commissioning_report(station_name: str = "") -> CommissioningReport:
    state = read_commissioning_state()
    if state.first_run_checks is None or state.channel_setup is None or state.proof_run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Run the first-run checks, channel setup, and output proof steps before the report.",
        )
    report = build_commissioning_report(
        station_name=station_name,
        first_run_checks=state.first_run_checks,
        channel_setup=state.channel_setup,
        proof_run=state.proof_run,
    )
    save_commissioning_report(report)
    return report
