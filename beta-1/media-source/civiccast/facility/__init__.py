# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Broadcast facility integration contracts."""

from civiccast.facility.models import (
    ROUTER_VENDOR_BLACKMAGIC,
    ROUTER_VENDOR_EVERTZ,
    ROUTER_VENDOR_GENERIC,
    ROUTER_VENDOR_ROSS,
    ROUTER_VENDOR_UTAH,
    RouterEndpoint,
    RouterInput,
    RouterInventory,
    RouterOutput,
    RouterScheduledTakePlan,
    RouterScheduledTakePreviewRequest,
    RouterScheduledTakeRequest,
    RouterTakePlan,
    RouterTakePreviewRequest,
    RouterTakeRequest,
    VirtualRouterButton,
    VirtualRouterPanel,
)
from civiccast.facility.router_control import (
    build_router_scheduled_take_plan,
    build_router_take_plan,
    build_virtual_router_panel,
)

__all__ = [
    "ROUTER_VENDOR_BLACKMAGIC",
    "ROUTER_VENDOR_EVERTZ",
    "ROUTER_VENDOR_GENERIC",
    "ROUTER_VENDOR_ROSS",
    "ROUTER_VENDOR_UTAH",
    "RouterEndpoint",
    "RouterInput",
    "RouterInventory",
    "RouterOutput",
    "RouterScheduledTakePlan",
    "RouterScheduledTakePreviewRequest",
    "RouterScheduledTakeRequest",
    "RouterTakePlan",
    "RouterTakePreviewRequest",
    "RouterTakeRequest",
    "VirtualRouterButton",
    "VirtualRouterPanel",
    "build_router_scheduled_take_plan",
    "build_router_take_plan",
    "build_virtual_router_panel",
]
