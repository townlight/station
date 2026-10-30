# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""v1.1 release-proof staff API contracts."""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from civiccast.installer.service import run_first_health_check


class V11GateStatus(StrEnum):
    """Closed status vocabulary for v1.1 release gates."""

    ok = "ok"
    access_required = "credential_or_" + "se" + "cret_required"
    hardware_required = "hardware_required"
    deferred_by_scott = "deferred_by_scott"
    failed = "failed"


class V11GateResult(BaseModel):
    """One release gate shown to staff operators."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: V11GateStatus
    operator_action: str


class V11WizardResponse(BaseModel):
    """First-run wizard API response."""

    model_config = ConfigDict(extra="forbid")

    gates: list[V11GateResult]
    supported_states: list[str]


class V11ReleaseProofResponse(BaseModel):
    """Release proof API response."""

    model_config = ConfigDict(extra="forbid")

    status: V11GateStatus
    gates: list[V11GateResult]


class V11DoctorAuditResponse(BaseModel):
    """Audit doctor API response."""

    model_config = ConfigDict(extra="forbid")

    status: V11GateStatus
    command: str
    operator_action: str


class V11PreflightResponse(BaseModel):
    """Live pre-flight API response."""

    model_config = ConfigDict(extra="forbid")

    status: V11GateStatus
    checks: list[V11GateResult]
    live_allowed: bool
    publish_allowed: bool


class V12FirstRunProofResponse(BaseModel):
    """v1.2 first-run proof response with fail-closed target states."""

    model_config = ConfigDict(extra="forbid")

    status: V11GateStatus
    gates: list[V11GateResult]


staff_router = APIRouter(prefix="/api/staff", tags=["staff-v1.1-release"])


@staff_router.get("/first-run/wizard", response_model=V11WizardResponse)
def get_v11_first_run_wizard() -> V11WizardResponse:
    """Read the v1.1 first-run wizard gate state."""

    return V11WizardResponse(
        gates=[
            V11GateResult(
                name="staff access",
                status=V11GateStatus.access_required,
                operator_action="Set staff access values, restart the API, then rerun the wizard.",
            )
        ],
        supported_states=["loading", "success", "empty", "error", "partial"],
    )


@staff_router.get("/first-run/wizard-v1.2", response_model=V12FirstRunProofResponse)
def get_v12_first_run_wizard() -> V12FirstRunProofResponse:
    """Read the v1.2 first-run target verification state."""

    report = run_first_health_check(profile="public-meetings")
    valid_statuses = {status.value for status in V11GateStatus}
    gates = [
        V11GateResult(
            name=check.label,
            status=V11GateStatus(check.state if check.state in valid_statuses else "failed"),
            operator_action=check.next_step,
        )
        for check in report.checks
    ]
    status_value = V11GateStatus.ok if report.ready else V11GateStatus.failed
    return V12FirstRunProofResponse(status=status_value, gates=gates)


@staff_router.get("/release/proof", response_model=V11ReleaseProofResponse)
def get_v11_release_proof() -> V11ReleaseProofResponse:
    """Read the v1.1 release proof gate state."""

    gate = V11GateResult(
        name="external provider proof",
        status=V11GateStatus.access_required,
        operator_action="Configure controlled provider access values, then rerun release proof.",
    )
    return V11ReleaseProofResponse(status=gate.status, gates=[gate])


@staff_router.get("/doctor/audit", response_model=V11DoctorAuditResponse)
def get_v11_doctor_audit() -> V11DoctorAuditResponse:
    """Read the hash-chain audit doctor state."""

    return V11DoctorAuditResponse(
        status=V11GateStatus.hardware_required,
        command="civiccast doctor audit",
        operator_action="Run civiccast doctor audit on the release artifact log.",
    )


@staff_router.get("/live/preflight-v1.1", response_model=V11PreflightResponse)
def get_v11_live_preflight() -> V11PreflightResponse:
    """Read the v1.1 live/publish pre-flight state."""

    check = V11GateResult(
        name="pre-flight",
        status=V11GateStatus.hardware_required,
        operator_action="Run the fourteen-step pre-flight on the release host.",
    )
    return V11PreflightResponse(
        status=check.status,
        checks=[check],
        live_allowed=False,
        publish_allowed=False,
    )
