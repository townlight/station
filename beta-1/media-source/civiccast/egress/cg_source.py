# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CG source contract for egress overlay rendering and proof."""

from __future__ import annotations

from civiccast.egress.branding import (
    BRANDING_PROOF_BOUNDARY,
    EgressBrandingPlan,
    EgressOverlayRegion,
    build_branding_filter_plan,
)
from civiccast.egress.cg_bridge import (
    CG_EGRESS_PROOF_BOUNDARY,
    EgressCgOverlayClearProof,
    EgressCgOverlayProof,
    build_cg_overlay_clear_egress_proof,
    build_cg_overlay_egress_proof,
)

__all__ = [
    "BRANDING_PROOF_BOUNDARY",
    "CG_EGRESS_PROOF_BOUNDARY",
    "EgressBrandingPlan",
    "EgressCgOverlayClearProof",
    "EgressCgOverlayProof",
    "EgressOverlayRegion",
    "build_branding_filter_plan",
    "build_cg_overlay_clear_egress_proof",
    "build_cg_overlay_egress_proof",
]
