# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Egress proof bridge for CivicCast CG overlay contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.cg.models import CgOverlayContract, EmergencyOverlay

CG_EGRESS_PROOF_BOUNDARY: Literal["civiccast-cg-overlay-to-egress-proof-boundary"] = (
    "civiccast-cg-overlay-to-egress-proof-boundary"
)
CG_EGRESS_NOT_CLAIMED = (
    "This proof records CivicCast CG overlay intent only.",
    "This proof does not claim EAS origination, EAS certification, CAP relay, or alert authority.",
    "This proof does not validate downstream headend rendering.",
)


class EgressCgOverlayProof(BaseModel):
    """Machine-readable proof that egress received a real CG overlay contract."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["READY", "BLOCKED"]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    overlay_id: Annotated[str, Field(min_length=1, max_length=120)]
    severity: Literal["watch", "warning", "emergency"]
    overlay_title: Annotated[str, Field(min_length=1, max_length=160)]
    overlay_contract_format: Literal["json-overlay-v1"]
    overlay_contract_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    egress_proof_boundary: Literal["civiccast-cg-overlay-to-egress-proof-boundary"]
    safe_area_percent: Annotated[int, Field(ge=0, le=20)]
    zone_count: Annotated[int, Field(ge=0, le=20)]
    operator_label: Literal["CivicCast emergency banner"]
    eas_claim: Literal["not_eas"]
    blocker: str | None
    next_step: Annotated[str, Field(min_length=1, max_length=500)]
    not_claimed: tuple[str, ...] = CG_EGRESS_NOT_CLAIMED


class EgressCgOverlayClearProof(BaseModel):
    """Machine-readable proof that an emergency CG overlay was cleared at egress."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["CLEARED"]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    overlay_id: Annotated[str, Field(min_length=1, max_length=120)]
    egress_proof_boundary: Literal["civiccast-cg-overlay-to-egress-proof-boundary"]
    operator_label: Literal["CivicCast emergency banner cleared"]
    eas_claim: Literal["not_eas"]
    next_step: Annotated[str, Field(min_length=1, max_length=500)]
    not_claimed: tuple[str, ...] = CG_EGRESS_NOT_CLAIMED


def build_cg_overlay_egress_proof(
    *,
    overlay: EmergencyOverlay,
    overlay_contract: CgOverlayContract,
) -> EgressCgOverlayProof:
    """Build a fail-closed proof that an overlay can enter the egress evidence path."""

    blocker = None
    if overlay_contract.zone_count < 1:
        blocker = "EGRESS_CG_OVERLAY_CONTRACT_HAS_NO_ZONES"
    status: Literal["READY", "BLOCKED"] = "READY" if blocker is None else "BLOCKED"
    return EgressCgOverlayProof(
        status=status,
        channel_id=overlay_contract.channel_id,
        overlay_id=overlay.overlay_id,
        severity=overlay.severity,
        overlay_title=overlay.title,
        overlay_contract_format=overlay_contract.format,
        overlay_contract_boundary=overlay_contract.proof_boundary,
        egress_proof_boundary=CG_EGRESS_PROOF_BOUNDARY,
        safe_area_percent=overlay_contract.safe_area_percent,
        zone_count=overlay_contract.zone_count,
        operator_label="CivicCast emergency banner",
        eas_claim="not_eas",
        blocker=blocker,
        next_step=(
            "Record this CG overlay proof in egress evidence; do not describe it as EAS."
            if blocker is None
            else "Fix the CG overlay contract before attaching it to egress evidence."
        ),
    )


def build_cg_overlay_clear_egress_proof(
    *,
    channel_id: str,
    overlay_id: str,
) -> EgressCgOverlayClearProof:
    """Build an explicit proof that egress cleared an emergency banner."""

    return EgressCgOverlayClearProof(
        status="CLEARED",
        channel_id=channel_id,
        overlay_id=overlay_id,
        egress_proof_boundary=CG_EGRESS_PROOF_BOUNDARY,
        operator_label="CivicCast emergency banner cleared",
        eas_claim="not_eas",
        next_step=(
            "Record this CG overlay clear proof in egress evidence; do not describe it as EAS."
        ),
    )
