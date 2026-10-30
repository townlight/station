# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Six-hour live/caption/egress/summary/publish soak contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class SixHourSoakPlan:
    """Release soak plan."""

    duration: timedelta
    stages: list[str]
    uses_live_state_machine: bool


@dataclass(frozen=True)
class SoakEvidenceResult:
    """Rendered soak evidence result."""

    status: str
    operator_action: str


def build_six_hour_soak_plan(duration: timedelta) -> SixHourSoakPlan:
    """Build the full release soak plan."""

    return SixHourSoakPlan(
        duration=duration,
        stages=["live", "caption", "egress", "summary", "publish"],
        uses_live_state_machine=True,
    )


def render_soak_evidence(
    *,
    duration: timedelta,
    release_mode: bool,
) -> SoakEvidenceResult:
    """Refuse to label shortened release-mode runs as six-hour evidence."""

    if release_mode and duration < timedelta(hours=6):
        return SoakEvidenceResult(
            status="failed",
            operator_action="A shortened run cannot be reported as six-hour release evidence.",
        )
    return SoakEvidenceResult(
        status="ok",
        operator_action="Soak duration satisfies the release evidence label.",
    )
