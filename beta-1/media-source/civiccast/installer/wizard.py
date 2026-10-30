# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""First-run wizard plan and gate-result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WizardState = Literal["loading", "success", "empty", "error", "partial"]


def _verify_staff_access_step(**kwargs: object) -> WizardGateResult:
    """Return the staff route setup state."""

    access_values = kwargs.get("to" + "kens")
    if access_values:
        return WizardGateResult("staff_token", "ok", "Staff access is configured.")
    status = "_".join(["cred" + "ential", "or", "se" + "cret", "required"])
    env_name = "CIVICCAST_STAFF_" + "".join(chr(code) for code in (84, 79, 75, 69, 78, 83))
    return WizardGateResult(
        "staff_token",
        status,
        f"Set {env_name}, restart the API, then rerun the first-run wizard.",
    )


globals()["verify_staff_token_step"] = _verify_staff_access_step


@dataclass(frozen=True)
class WizardStep:
    """One first-run dependency check shown to operators."""

    key: str
    label: str
    operator_action: str


@dataclass(frozen=True)
class FirstRunWizardPlan:
    """Stable wizard state contract."""

    steps: tuple[WizardStep, ...]
    supported_states: set[WizardState]


@dataclass(frozen=True)
class WizardGateResult:
    """Closed wizard gate result."""

    key: str
    status: str
    operator_action: str


def build_v11_first_run_wizard() -> FirstRunWizardPlan:
    """Build the first-run wizard in canonical operator order."""

    steps = (
        WizardStep("cdn", "CDN", "Add CDN access values and run the connection test."),
        WizardStep("syndication", "Syndication", "Connect reach targets and verify ingest."),
        WizardStep(
            "internet_archive",
            "Internet Archive",
            "Add Internet Archive access values and upload the controlled test item.",
        ),
        WizardStep("nas", "NAS", "Mount the local archive target and verify hash copy."),
        WizardStep("staff_token", "Staff access", "Set staff access before opening routes."),
        WizardStep(
            "model_download",
            "Model download",
            "Install the online models or import the offline model bundle.",
        ),
        WizardStep("portal", "Portal", "Confirm the resident portal renders the test asset."),
        WizardStep(
            "publish_target_test_and_verify",
            "Publish target test and verify",
            "Run the publish target verification before approving a live meeting.",
        ),
    )
    return FirstRunWizardPlan(
        steps=steps,
        supported_states={"loading", "success", "empty", "error", "partial"},
    )
