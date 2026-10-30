# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CG and branding overlay plans for egress rendering."""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from civiccast.cg.models import CgOverlayContract, CgTemplateZone, EmergencyOverlay
from civiccast.egress.models import CanonicalProfile

BRANDING_PROOF_BOUNDARY = "cg-overlay-contract-to-egress-filter-plan"


class EgressOverlayRegion(BaseModel):
    """One CG contract region mapped to output-frame coordinates."""

    model_config = ConfigDict(extra="forbid")

    region: str
    zone_kind: str
    order: int
    x: int
    y: int
    width: int
    height: int


class EgressBrandingPlan(BaseModel):
    """FFmpeg filter plan generated from the CG overlay contract."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    overlay_source_url: Annotated[str, Field(min_length=1, max_length=500)]
    overlay_contract_format: Literal["json-overlay-v1"]
    overlay_contract_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)]
    safe_area_pixels: int
    regions: list[EgressOverlayRegion]
    filter_complex: Annotated[str, Field(min_length=1)]
    output_video_label: Annotated[str, Field(min_length=1, max_length=80)]
    emergency_overlay_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    operator_label: Annotated[str, Field(min_length=1, max_length=120)] = "CivicCast CG overlay"
    eas_claim: Literal["not_eas"] = "not_eas"
    not_claimed: list[str] = Field(default_factory=list)


def build_branding_filter_plan(
    *,
    overlay_contract: CgOverlayContract,
    profile: CanonicalProfile | None = None,
    emergency_overlay: EmergencyOverlay | None = None,
    snapshot_base_url: str | None = None,
) -> EgressBrandingPlan:
    """Build a deterministic FFmpeg overlay filter plan from real CG contracts."""

    active_profile = profile or CanonicalProfile()
    overlay_source_url = _ffmpeg_overlay_source(
        overlay_contract.snapshot_url,
        snapshot_base_url=snapshot_base_url,
    )
    safe_area_pixels = round(
        min(active_profile.width, active_profile.height)
        * (overlay_contract.safe_area_percent / 100)
    )
    regions = [
        _region_from_zone(zone, profile=active_profile, safe_area_pixels=safe_area_pixels)
        for zone in _ordered_regions(overlay_contract.regions, emergency_overlay=emergency_overlay)
    ]
    if not regions:
        raise ValueError("overlay_contract must include at least one region")
    return EgressBrandingPlan(
        channel_id=overlay_contract.channel_id,
        overlay_source_url=overlay_source_url,
        overlay_contract_format=overlay_contract.format,
        overlay_contract_boundary=overlay_contract.proof_boundary,
        proof_boundary=BRANDING_PROOF_BOUNDARY,
        safe_area_pixels=safe_area_pixels,
        regions=regions,
        filter_complex=_build_filter_complex(
            overlay_source_url,
            regions=regions,
        ),
        output_video_label=f"v_cg_{len(regions)}",
        emergency_overlay_id=emergency_overlay.overlay_id if emergency_overlay else None,
        operator_label=(
            "CivicCast emergency banner" if emergency_overlay else "CivicCast CG overlay"
        ),
        not_claimed=[
            "This plan renders CG overlays at CivicCast's egress filter boundary only.",
            "This plan does not claim FCC EAS origination, ENDEC control, or EAS certification.",
            "This plan does not prove downstream headend rendering until an emitted stream is tested.",
        ],
    )


def _ordered_regions(
    regions: list[CgTemplateZone],
    *,
    emergency_overlay: EmergencyOverlay | None,
) -> list[CgTemplateZone]:
    ordered = sorted(regions, key=lambda zone: (zone.order, zone.region, zone.zone_kind))
    if emergency_overlay is None:
        return ordered
    alert_regions = [zone for zone in ordered if zone.zone_kind == "alert"]
    other_regions = [zone for zone in ordered if zone.zone_kind != "alert"]
    if not alert_regions:
        raise ValueError("emergency overlays require an alert region in the CG contract")
    return [*other_regions, *alert_regions]


def _region_from_zone(
    zone: CgTemplateZone,
    *,
    profile: CanonicalProfile,
    safe_area_pixels: int,
) -> EgressOverlayRegion:
    x, y, width, height = _region_box(
        zone.region,
        profile=profile,
        safe_area_pixels=safe_area_pixels,
    )
    return EgressOverlayRegion(
        region=zone.region,
        zone_kind=zone.zone_kind,
        order=zone.order,
        x=x,
        y=y,
        width=width,
        height=height,
    )


def _region_box(
    region: str,
    *,
    profile: CanonicalProfile,
    safe_area_pixels: int,
) -> tuple[int, int, int, int]:
    safe = safe_area_pixels
    width = profile.width
    height = profile.height
    if region == "main":
        return safe, safe, max(1, width - (safe * 2)), max(1, height - (safe * 2))
    if region == "lower":
        lower_height = max(1, height // 5)
        return safe, height - safe - lower_height, max(1, width - (safe * 2)), lower_height
    if region == "side":
        side_width = max(1, width // 4)
        return width - safe - side_width, safe, side_width, max(1, height - (safe * 2))
    if region == "bug":
        bug_size = max(1, min(width, height) // 8)
        return width - safe - bug_size, safe, bug_size, bug_size
    if region == "background":
        return 0, 0, width, height
    raise ValueError(f"unsupported CG template region: {region}")


def _build_filter_complex(
    snapshot_url: str,
    *,
    regions: list[EgressOverlayRegion],
) -> str:
    parts: list[str] = []
    previous = "0:v"
    for index, region in enumerate(regions, start=1):
        movie_label = f"cg{index}"
        output_label = f"v_cg_{index}"
        parts.append(
            f"movie='{_escape_filter_value(snapshot_url)}',"
            f"scale={region.width}:{region.height}[{movie_label}]"
        )
        parts.append(
            f"[{previous}][{movie_label}]overlay=x={region.x}:y={region.y}[{output_label}]"
        )
        previous = output_label
    return ";".join(parts)


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _ffmpeg_overlay_source(value: str, *, snapshot_base_url: str | None) -> str:
    parsed = urlsplit(value)
    if parsed.scheme or not value.startswith("/"):
        return value
    if snapshot_base_url is None:
        return value
    return urljoin(snapshot_base_url.rstrip("/") + "/", value.lstrip("/"))
