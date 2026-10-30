# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""civiccast.stream — HLS streaming origin module.

Sprint 0.2 ships the VOD path only (live ingest is Sprint 0.4).

Public API surface::

    from civiccast.stream import pack_vod_asset, pack_slate_fallback
    from civiccast.stream import PackagingError, VodPackageResult
    from civiccast.stream.cdn import CDNAdapter
    from civiccast.stream.cdn.bunny import BunnyCDNAdapter
    from civiccast.stream.cdn.stub import StubCDNAdapter

See civiccast/stream/README.md for operator-oriented documentation.
See docs/adr/0007-hls-packager-design.md for design rationale.
See docs/adr/0006-cdn-provider.md for CDN choice rationale.
"""

from civiccast.stream.manifest import ManifestSubtitleTrack
from civiccast.stream.overlays import (
    OVERLAY_Z_ORDER,
    OverlayCompositorPlan,
    OverlayCompositorRequest,
    OverlayGeometry,
    OverlayLayer,
    build_overlay_compositor_plan,
    default_squeezeback_lbar_layers,
    select_acceleration_mode,
)
from civiccast.stream.packager import (
    PackagingError,
    RenditionOutput,
    SlateOnlyResult,
    VodPackageResult,
    pack_slate_fallback,
    pack_vod_asset,
)

__all__ = [
    "OVERLAY_Z_ORDER",
    "ManifestSubtitleTrack",
    "OverlayCompositorPlan",
    "OverlayCompositorRequest",
    "OverlayGeometry",
    "OverlayLayer",
    "PackagingError",
    "RenditionOutput",
    "SlateOnlyResult",
    "VodPackageResult",
    "build_overlay_compositor_plan",
    "default_squeezeback_lbar_layers",
    "pack_slate_fallback",
    "pack_vod_asset",
    "select_acceleration_mode",
]
