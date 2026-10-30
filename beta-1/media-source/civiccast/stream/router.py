# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API routes for stream output planning."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from civiccast.auth.roles import require_any_role
from civiccast.stream.overlays import (
    OverlayCompositorPlan,
    OverlayCompositorRequest,
    build_overlay_compositor_plan,
)

staff_router = APIRouter(prefix="/api/staff/stream", tags=["staff", "stream"])


def get_ffmpeg_encoders_output() -> str | None:
    """Dependency seam for local ffmpeg encoder capability text."""

    return None


@staff_router.post(
    "/overlay-compositor-plan",
    response_model=OverlayCompositorPlan,
    summary="Preview a streaming overlay compositor plan",
    dependencies=[Depends(require_any_role("meeting_operator", "support_admin"))],
)
def overlay_compositor_plan(
    payload: OverlayCompositorRequest,
    ffmpeg_encoders_output: str | None = Depends(get_ffmpeg_encoders_output),
) -> OverlayCompositorPlan:
    return build_overlay_compositor_plan(
        payload,
        ffmpeg_encoders_output=ffmpeg_encoders_output,
    )
