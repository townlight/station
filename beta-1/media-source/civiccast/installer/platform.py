# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Cross-platform installer bootstrap contracts.

Covers the Linux (systemd) and macOS (launchd) native deployments only.
Windows deployment readiness is decided separately, by
``civiccast.installer.service``'s native-station detection
(``_native_windows_station`` / ``_native_station_activated``) -- the
retired WSL2/Ubuntu lane this module used to also model for Windows was
removed under the owner's "no linux" decision (2026-08-19); Windows never
had a native-vs-WSL choice to reject here, it has its own dedicated,
non-generic bootstrap path.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.installer.models import BootstrapMetadata, ServiceMetadata

OsFamily = Literal["linux", "macos"]
BootstrapStatus = Literal["ready", "blocked"]


class PlatformBootstrapPlan(BaseModel):
    """Fail-closed platform bootstrap plan for the Linux/macOS native
    deployments."""

    model_config = ConfigDict(extra="forbid")

    os_family: OsFamily
    runtime: Annotated[str, Field(min_length=1)]
    status: BootstrapStatus
    service_metadata: ServiceMetadata
    bootstrap_metadata: BootstrapMetadata
    blockers: list[str]
    next_step: Annotated[str, Field(min_length=1)]


def build_bootstrap_plan(
    *, os_family: OsFamily, detected_tools: dict[str, object] | None = None
) -> PlatformBootstrapPlan:
    """Build an actionable bootstrap plan for one platform family."""

    tools = detected_tools or {}
    if os_family == "linux":
        if not tools.get("systemd"):
            return PlatformBootstrapPlan(
                os_family="linux",
                runtime="native-linux",
                status="blocked",
                service_metadata=ServiceMetadata(manager="systemd"),
                bootstrap_metadata=BootstrapMetadata(
                    package_kind="deb",
                    package_manager=str(tools.get("package_manager") or "apt"),
                ),
                blockers=["systemd service manager is unavailable."],
                next_step="Install on a Linux host with systemd, then run systemctl enable civiccast.",
            )
        package_manager = str(tools.get("package_manager") or "apt")
        return PlatformBootstrapPlan(
            os_family="linux",
            runtime="native-linux",
            status="ready",
            service_metadata=ServiceMetadata(manager="systemd"),
            bootstrap_metadata=BootstrapMetadata(
                package_kind="deb" if package_manager == "apt" else "rpm",
                package_manager=package_manager,
            ),
            blockers=[],
            next_step="Install the package, then run systemctl enable civiccast --now.",
        )

    ready = bool(tools.get("launchd")) and bool(tools.get("pkgbuild"))
    return PlatformBootstrapPlan(
        os_family="macos",
        runtime="native-macos",
        status="ready" if ready else "blocked",
        service_metadata=ServiceMetadata(manager="launchd"),
        bootstrap_metadata=BootstrapMetadata(package_kind="pkg"),
        blockers=[] if ready else ["launchd and pkgbuild tooling are required."],
        next_step=(
            "Install the signed .pkg, then load org.civiccast.civiccast with launchctl."
            if ready
            else "Install Xcode command line packaging tools, then rebuild the .pkg."
        ),
    )
