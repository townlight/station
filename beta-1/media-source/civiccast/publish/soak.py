# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic publish integration soak runner for v1.0 readiness."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.publish.models import PublishApprovalRequest, PublishPreflightResponse
from civiccast.publish.service import (
    approve_publish,
    build_publish_asset_status,
    build_publish_preflight,
)
from civiccast.publish.store import InMemoryPublishStore
from civiccast.schedule.models import StaffAssetRow

PublishTier = Literal["canonical", "archive", "reach", "audience"]

EXPECTED_SURFACE_IDS = (
    "portal",
    "internet-archive",
    "local-nas-rsync",
    "local-nas-zfs",
    "youtube-live",
    "youtube-vod",
    "podcast",
    "subscriber-notifications",
)

EXPECTED_REQUIRED_SURFACE_IDS = (
    "portal",
    "internet-archive",
    "local-nas-rsync",
    "local-nas-zfs",
)


class PublishSoakIteration(BaseModel):
    """One completed publish cycle in the nightly soak proof."""

    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    asset_id: str
    ready: bool
    dashboard_state: str
    canonical_public: bool
    archive_verified: bool
    required_surface_ids: list[str]
    succeeded_surface_ids: list[str]
    verified_archive_hashes: dict[str, str]
    proof_urls_or_paths: dict[str, str]
    audit_event_count: int


class PublishSoakResult(BaseModel):
    """Machine-readable nightly publish + soak evidence."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    profile: str
    iterations_requested: int = Field(ge=1)
    iterations_completed: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    status: Literal["passed", "failed"]
    tier_surface_ids: dict[PublishTier, list[str]]
    iterations: list[PublishSoakIteration]


def _asset_for_iteration(iteration: int) -> StaffAssetRow:
    asset_id = f"nightly-publish-soak-{iteration:03d}"
    return StaffAssetRow(
        asset_id=asset_id,
        title=f"Nightly Publish Soak {iteration:03d}",
        state="validated",
        manifest_url=f"https://cdn.example/{asset_id}/playlist.m3u8",
        published_at=datetime(2026, 5, 15, 4, 0, tzinfo=UTC),
        retention_policy="meeting",
        version=1,
    )


def _assert_preflight_ready(preflight: PublishPreflightResponse) -> None:
    if not preflight.ready:
        failed = ", ".join(
            f"{check.id}:{check.health}" for check in preflight.checks if check.health != "ok"
        )
        raise RuntimeError(f"Publish soak preflight failed: {failed}")


def _proof_value(url: str | None, path: str | None) -> str | None:
    return url if url is not None else path


def run_publish_soak(iterations: int = 24) -> PublishSoakResult:
    """Run repeated deterministic publish approvals across every v1.0 surface."""
    if iterations < 1:
        raise ValueError("iterations must be at least 1")

    started = monotonic()
    store = InMemoryPublishStore()
    completed: list[PublishSoakIteration] = []
    tier_surface_ids: dict[PublishTier, list[str]] = {
        "canonical": ["portal"],
        "archive": ["internet-archive", "local-nas-rsync", "local-nas-zfs"],
        "reach": ["youtube-live", "youtube-vod"],
        "audience": ["podcast", "subscriber-notifications"],
    }

    for iteration in range(1, iterations + 1):
        asset = _asset_for_iteration(iteration)
        preflight = build_publish_preflight(asset)
        _assert_preflight_ready(preflight)
        record = approve_publish(
            asset=asset,
            # The soak exercises every surface on purpose; an omitted
            # selection means the canonical Portal surface only (F-23).
            request=PublishApprovalRequest(
                operator_id="nightly-soak",
                operator_display_name="Nightly Publish Soak",
                approved_surface_ids=list(EXPECTED_SURFACE_IDS),
            ),
            store=store,
        )
        status = build_publish_asset_status(asset, record)
        surfaces = {surface.id: surface for surface in status.surfaces}
        missing = [surface_id for surface_id in EXPECTED_SURFACE_IDS if surface_id not in surfaces]
        if missing:
            raise RuntimeError(f"Publish soak missing expected surfaces: {', '.join(missing)}")

        succeeded_surface_ids = [
            surface_id
            for surface_id in EXPECTED_SURFACE_IDS
            if surfaces[surface_id].state == "succeeded"
        ]
        required_surface_ids = [
            surface_id
            for surface_id in EXPECTED_REQUIRED_SURFACE_IDS
            if surfaces[surface_id].required
        ]
        failed_required = [
            surface_id
            for surface_id in required_surface_ids
            if surfaces[surface_id].state != "succeeded"
        ]
        if failed_required:
            raise RuntimeError(
                "Publish soak required surfaces failed: " + ", ".join(failed_required)
            )
        if not status.canonical_public or not status.archive_verified:
            raise RuntimeError(
                f"Publish soak did not verify portal/archive for {asset.asset_id}: "
                f"canonical_public={status.canonical_public}, "
                f"archive_verified={status.archive_verified}"
            )

        verified_archive_hashes = {
            surface_id: surfaces[surface_id].verification_hash or ""
            for surface_id in ("internet-archive", "local-nas-rsync", "local-nas-zfs")
        }
        if any(not value.startswith("sha256:") for value in verified_archive_hashes.values()):
            raise RuntimeError(f"Publish soak missing archive hash proof for {asset.asset_id}")

        proof_urls_or_paths = {
            surface_id: value
            for surface_id in EXPECTED_SURFACE_IDS
            if (value := _proof_value(surfaces[surface_id].url, surfaces[surface_id].path))
            is not None
        }

        completed.append(
            PublishSoakIteration(
                iteration=iteration,
                asset_id=asset.asset_id,
                ready=preflight.ready,
                dashboard_state=status.dashboard_state,
                canonical_public=status.canonical_public,
                archive_verified=status.archive_verified,
                required_surface_ids=required_surface_ids,
                succeeded_surface_ids=succeeded_surface_ids,
                verified_archive_hashes=verified_archive_hashes,
                proof_urls_or_paths=proof_urls_or_paths,
                audit_event_count=len(record.audit_events),
            )
        )

    return PublishSoakResult(
        generated_at=datetime.now(UTC),
        profile="public-meetings",
        iterations_requested=iterations,
        iterations_completed=len(completed),
        duration_seconds=round(monotonic() - started, 3),
        status="passed",
        tier_surface_ids=tier_surface_ids,
        iterations=completed,
    )
