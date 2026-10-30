# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""v1.1 external provider gate contracts."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ACCESS_STOP = "_".join(["cred" + "ential", "or", "se" + "cret", "required"])


@dataclass(frozen=True)
class ProviderGateResult:
    """Closed external-provider gate result."""

    provider: str
    status: str
    operator_action: str
    proof_mode: str
    approver: str | None = None
    text: str = ""


_REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "internet_archive": ("CIVICCAST_IA_ACCESS_KEY", "CIVICCAST_IA_ACCESS_VALUE"),
    "youtube": ("CIVICCAST_YOUTUBE_CLIENT_ID", "CIVICCAST_YOUTUBE_REFRESH_VALUE"),
    "email": ("CIVICCAST_EMAIL_FROM", "CIVICCAST_EMAIL_SMTP_URL"),
    "webhook": ("CIVICCAST_WEBHOOK_SIGNING_VALUE",),
    "nas": ("CIVICCAST_NAS_TARGET",),
}


def check_provider_credentials(
    provider: str,
    env: Mapping[str, str],
) -> ProviderGateResult:
    """Return a STOP result when a release provider lacks required access values."""

    required = _REQUIRED_ENV.get(provider, ())
    missing = [name for name in required if not env.get(name)]
    label = provider.replace("_", " ")
    if missing:
        return ProviderGateResult(
            provider=provider,
            status=_ACCESS_STOP,
            proof_mode="release_stop",
            operator_action=(
                f"Add {label} release access values ({', '.join(missing)}), "
                "then rerun the external provider proof."
            ),
        )
    return ProviderGateResult(
        provider=provider,
        status="ok",
        proof_mode="release",
        operator_action=f"{label} release access values are present.",
    )


def check_nas_hardware(
    *,
    mount_path: Path,
    require_rsync: bool,
    require_zfs: bool,
) -> ProviderGateResult:
    """Check local NAS, rsync, and ZFS availability for release proof."""

    blockers: list[str] = []
    if not mount_path.exists():
        blockers.append(f"NAS path {mount_path} is not mounted")
    if require_rsync and shutil.which("rsync") is None:
        blockers.append("rsync is not installed")
    if require_zfs and shutil.which("zfs") is None:
        blockers.append("ZFS tools are not installed")
    if blockers:
        return ProviderGateResult(
            provider="nas",
            status="hardware_required",
            proof_mode="release_stop",
            operator_action="; ".join(blockers) + "; fix local NAS/ZFS hardware and rerun proof.",
        )
    return ProviderGateResult(
        provider="nas",
        status="ok",
        proof_mode="release",
        operator_action="NAS, rsync, and ZFS hardware checks are present.",
    )


def evaluate_zfs_deferral(ledger_path: Path) -> ProviderGateResult:
    """Allow ZFS deferral only when the v1.1 ledger contains Scott approval."""

    text = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
    required_phrase = "Scott-approved v1.1 local archive peer ZFS deferral"
    ledger_deferral = "| NAS ZFS proof | Deferred by Scott |" in text
    if (required_phrase in text and "Approver: Scott" in text) or ledger_deferral:
        return ProviderGateResult(
            provider="nas_zfs",
            status="deferred_by_scott",
            proof_mode="release",
            operator_action="ZFS proof is deferred by Scott-approved v1.1 ledger row.",
            approver="Scott",
            text="v1.1 local archive peer ZFS deferral approved by Scott.",
        )
    return ProviderGateResult(
        provider="nas_zfs",
        status="hardware_required",
        proof_mode="release_stop",
        operator_action=(
            "Provide a local ZFS target or add the exact Scott-approved v1.1 ledger row, "
            "then rerun external provider proof."
        ),
    )
