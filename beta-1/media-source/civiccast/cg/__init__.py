# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Character-generator idle, emergency, and multi-zone bulletin-board contracts."""

from civiccast.cg.models import (
    CgFeedCatalog,
    CgHlsRenderPlan,
    CgOverlayContract,
    CgPortalDisplay,
    CgTemplateLibrary,
    EmergencyOverlay,
    IdlePage,
    MultiZoneCgSnapshot,
)
from civiccast.cg.service import (
    build_emergency_overlay,
    build_feed_catalog,
    build_hls_manifest,
    build_hls_render_plan,
    build_idle_page,
    build_multi_zone_snapshot,
    build_overlay_contract,
    build_portal_display,
    build_template_library,
)

__all__ = [
    "CgFeedCatalog",
    "CgHlsRenderPlan",
    "CgOverlayContract",
    "CgPortalDisplay",
    "CgTemplateLibrary",
    "EmergencyOverlay",
    "IdlePage",
    "MultiZoneCgSnapshot",
    "build_emergency_overlay",
    "build_feed_catalog",
    "build_hls_manifest",
    "build_hls_render_plan",
    "build_idle_page",
    "build_multi_zone_snapshot",
    "build_overlay_contract",
    "build_portal_display",
    "build_template_library",
]
