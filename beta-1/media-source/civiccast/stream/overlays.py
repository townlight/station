# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Streaming overlay compositor planning for live HLS output."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OverlayLayerKind = Literal["squeezeback", "l-bar", "bug", "lower-third", "emergency"]
AccelerationMode = Literal["auto", "cpu", "nvenc", "vaapi"]

OVERLAY_Z_ORDER: dict[OverlayLayerKind, int] = {
    "squeezeback": 10,
    "l-bar": 20,
    "bug": 30,
    "lower-third": 40,
    "emergency": 90,
}

PROOF_BOUNDARY = "overlay-compositor-command-planning-no-ffmpeg-execution"


class OverlayGeometry(BaseModel):
    """Layer geometry expressed as output-frame percentages."""

    model_config = ConfigDict(extra="forbid")

    x_percent: Annotated[float, Field(ge=0, le=100)]
    y_percent: Annotated[float, Field(ge=0, le=100)]
    width_percent: Annotated[float, Field(gt=0, le=100)]
    height_percent: Annotated[float, Field(gt=0, le=100)]

    @model_validator(mode="after")
    def _fits_frame(self) -> OverlayGeometry:
        if self.x_percent + self.width_percent > 100:
            raise ValueError("overlay geometry exceeds output width")
        if self.y_percent + self.height_percent > 100:
            raise ValueError("overlay geometry exceeds output height")
        return self


class OverlayLayer(BaseModel):
    """One composited visual layer."""

    model_config = ConfigDict(extra="forbid")

    layer_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: OverlayLayerKind
    label: Annotated[str, Field(min_length=1, max_length=160)]
    geometry: OverlayGeometry
    content_ref: Annotated[str | None, Field(default=None, max_length=500)] = None
    opacity: Annotated[float, Field(ge=0, le=1)] = 1.0
    enabled: bool = True


class OverlayCompositorRequest(BaseModel):
    """Request to build a streaming overlay compositor command plan."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    input_url: Annotated[str, Field(min_length=1, max_length=500)]
    output_manifest_path: Annotated[str, Field(min_length=1, max_length=500)]
    width: Annotated[int, Field(gt=0, le=7680)] = 1920
    height: Annotated[int, Field(gt=0, le=4320)] = 1080
    target_duration_seconds: Annotated[int, Field(gt=0, le=60)] = 4
    acceleration_preference: AccelerationMode = "auto"
    layers: list[OverlayLayer]

    @model_validator(mode="after")
    def _layers_are_unique(self) -> OverlayCompositorRequest:
        ids = [layer.layer_id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ValueError("overlay layer IDs must be unique")
        return self


class OverlayCompositorPlan(BaseModel):
    """Inspectable HLS overlay compositor plan."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    acceleration_mode: Literal["cpu", "nvenc", "vaapi"]
    gpu_accelerated: bool
    ordered_layers: list[OverlayLayer]
    z_order: dict[OverlayLayerKind, int]
    filter_complex: Annotated[str, Field(min_length=1, max_length=5000)]
    ffmpeg_args: list[str]
    proof_boundary: str = PROOF_BOUNDARY
    operator_action: str = (
        "Preview the overlay plan, then start the compositor from the live output panel."
    )


def build_overlay_compositor_plan(
    request: OverlayCompositorRequest,
    *,
    ffmpeg_encoders_output: str | None = None,
) -> OverlayCompositorPlan:
    """Build a GPU-aware HLS overlay command plan without invoking ffmpeg."""

    mode = select_acceleration_mode(
        request.acceleration_preference,
        ffmpeg_encoders_output=ffmpeg_encoders_output,
    )
    ordered_layers = sorted(
        [layer for layer in request.layers if layer.enabled],
        key=lambda layer: (OVERLAY_Z_ORDER[layer.kind], layer.layer_id),
    )
    filter_complex = _filter_complex(request, ordered_layers)
    video_codec = {
        "cpu": "h264",
        "nvenc": "h264_nvenc",
        "vaapi": "h264_vaapi",
    }[mode]
    ffmpeg_args = [
        "-i",
        request.input_url,
        "-filter_complex",
        filter_complex,
        "-c:v",
        video_codec,
        "-f",
        "hls",
        "-hls_time",
        str(request.target_duration_seconds),
        request.output_manifest_path,
    ]
    return OverlayCompositorPlan(
        channel_id=request.channel_id,
        acceleration_mode=mode,
        gpu_accelerated=mode != "cpu",
        ordered_layers=ordered_layers,
        z_order=OVERLAY_Z_ORDER,
        filter_complex=filter_complex,
        ffmpeg_args=ffmpeg_args,
    )


def select_acceleration_mode(
    preference: AccelerationMode,
    *,
    ffmpeg_encoders_output: str | None = None,
) -> Literal["cpu", "nvenc", "vaapi"]:
    """Choose an available compositor acceleration mode from ffmpeg capability text."""

    capabilities = (ffmpeg_encoders_output or "").casefold()
    if preference == "cpu":
        return "cpu"
    if preference == "nvenc":
        return "nvenc" if "h264_nvenc" in capabilities else "cpu"
    if preference == "vaapi":
        return "vaapi" if "h264_vaapi" in capabilities else "cpu"
    if "h264_nvenc" in capabilities:
        return "nvenc"
    if "h264_vaapi" in capabilities:
        return "vaapi"
    return "cpu"


def default_squeezeback_lbar_layers() -> list[OverlayLayer]:
    """Return a phone-triggerable squeezeback and L-bar template set."""

    return [
        OverlayLayer(
            layer_id="squeezeback-main",
            kind="squeezeback",
            label="Squeezeback main video",
            geometry=OverlayGeometry(
                x_percent=4,
                y_percent=4,
                width_percent=68,
                height_percent=72,
            ),
        ),
        OverlayLayer(
            layer_id="lbar-message",
            kind="l-bar",
            label="L-bar message well",
            geometry=OverlayGeometry(
                x_percent=0,
                y_percent=76,
                width_percent=100,
                height_percent=24,
            ),
            content_ref="cg://approved-bulletin-or-sponsor",
            opacity=0.92,
        ),
    ]


def _filter_complex(request: OverlayCompositorRequest, layers: list[OverlayLayer]) -> str:
    if not layers:
        return "[0:v]null[composited]"
    filters: list[str] = []
    input_label = "0:v"
    for index, layer in enumerate(layers):
        output_label = f"v{index}"
        filters.append(_layer_filter(request, layer, input_label, output_label))
        input_label = output_label
    filters.append(f"[{input_label}]format=yuv420p[composited]")
    return ";".join(filters)


def _layer_filter(
    request: OverlayCompositorRequest,
    layer: OverlayLayer,
    input_label: str,
    output_label: str,
) -> str:
    x, y, width, height = _pixel_geometry(request, layer.geometry)
    if layer.kind == "squeezeback":
        return f"[{input_label}]scale={width}:{height},pad={request.width}:{request.height}:{x}:{y}:black[{output_label}]"
    color = {
        "l-bar": "0x111827",
        "bug": "0x0F5E9C",
        "lower-third": "0x1F2937",
        "emergency": "0xB91C1C",
        "squeezeback": "black",
    }[layer.kind]
    return (
        f"[{input_label}]drawbox=x={x}:y={y}:w={width}:h={height}:"
        f"color={color}@{layer.opacity}:t=fill[{output_label}]"
    )


def _pixel_geometry(
    request: OverlayCompositorRequest,
    geometry: OverlayGeometry,
) -> tuple[int, int, int, int]:
    return (
        round(request.width * geometry.x_percent / 100),
        round(request.height * geometry.y_percent / 100),
        round(request.width * geometry.width_percent / 100),
        round(request.height * geometry.height_percent / 100),
    )
