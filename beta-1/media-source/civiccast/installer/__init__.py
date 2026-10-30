# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Installer and first-run contracts for CivicCast."""

from civiccast.installer.handoff import build_beta_handoff_summary
from civiccast.installer.models import (
    BetaHandoffLane,
    BetaHandoffSummary,
    DeploymentProfile,
    FirstRunHealthReport,
    FirstRunPlan,
    InstallerStep,
    ModelBundleManifest,
    ModelBundleRequest,
)
from civiccast.installer.service import (
    build_first_run_plan,
    build_model_bundle_manifest,
    run_first_health_check,
)

__all__ = [
    "BetaHandoffLane",
    "BetaHandoffSummary",
    "DeploymentProfile",
    "FirstRunHealthReport",
    "FirstRunPlan",
    "InstallerStep",
    "ModelBundleManifest",
    "ModelBundleRequest",
    "build_beta_handoff_summary",
    "build_first_run_plan",
    "build_model_bundle_manifest",
    "run_first_health_check",
]
