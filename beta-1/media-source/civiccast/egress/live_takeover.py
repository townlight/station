# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live ingest to egress source-plan adapter."""

from __future__ import annotations

from civiccast.egress.errors import SourcePrepareError
from civiccast.egress.models import EgressSourcePlan, EgressSourceSegment
from civiccast.live.models import RELAY_HEALTH_READY, LiveIngestPath, LiveIngestPlan


def build_live_takeover_source_plan(
    *,
    channel_id: str,
    ingest_plan: LiveIngestPlan,
    path_id: str | None = None,
    duration_seconds: float = 3600.0,
) -> EgressSourcePlan:
    """Return a one-segment live source plan from a staff-safe ingest plan.

    The live subsystem already removes credentials from operator-facing plans.
    This adapter consumes only that safe plan and keeps live takeover inside the
    same egress source-plan contract used by reload/handback proof events.
    """

    if ingest_plan.channel_id != channel_id:
        raise SourcePrepareError(
            f"Live ingest plan channel {ingest_plan.channel_id!r} does not match "
            f"egress channel {channel_id!r}."
        )
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero.")

    live_path = _select_live_path(ingest_plan, path_id=path_id)
    if not live_path.enabled:
        raise SourcePrepareError(f"Live ingest path {live_path.path_id!r} is disabled.")
    if live_path.health_state != RELAY_HEALTH_READY:
        raise SourcePrepareError(
            f"Live ingest path {live_path.path_id!r} is not ready: {live_path.health_state}."
        )
    source_url = live_path.return_playback_url or live_path.endpoint_url
    if not source_url:
        raise SourcePrepareError(
            f"Live ingest path {live_path.path_id!r} has no playback or endpoint URL."
        )
    return EgressSourcePlan(
        channel_id=channel_id,
        segments=[
            EgressSourceSegment(
                label=f"Live: {live_path.label}",
                path=source_url,
                duration_seconds=duration_seconds,
                kind="live",
                source_ref=live_path.path_id,
            )
        ],
    )


def _select_live_path(ingest_plan: LiveIngestPlan, *, path_id: str | None) -> LiveIngestPath:
    candidates = [ingest_plan.local_default, *ingest_plan.relay_paths]
    selected_id = path_id or ingest_plan.recommended_path_id
    for candidate in candidates:
        if candidate.path_id == selected_id:
            return candidate
    raise SourcePrepareError(f"Live ingest path {selected_id!r} was not found.")
