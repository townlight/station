# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Installer-facing model setup state machine."""

from __future__ import annotations

from pathlib import Path

from civiccast.installer.models import ModelSetupItem, ModelSetupResult
from civiccast.installer.packages import compute_sha256


def plan_online_model_setup(
    *,
    models: list[str],
    provider_available: bool,
    verified_hashes: dict[str, str] | None = None,
) -> list[ModelSetupItem]:
    """Return deterministic lifecycle states for online model setup."""

    if not provider_available:
        return [mark_model_unavailable(model, reason="provider unavailable") for model in models]
    label = ", ".join(models)
    verified_hashes = verified_hashes or {}
    missing_hashes = [model for model in models if model not in verified_hashes]
    final_state = (
        ModelSetupItem(
            name=label,
            status="complete",
            proof_state="hash_verified",
            sha256=next(iter(verified_hashes.values())),
            next_step="Model setup is complete; retain the hash verification proof.",
        )
        if not missing_hashes and verified_hashes
        else ModelSetupItem(
            name=label,
            status="unavailable",
            proof_state="proof_unavailable",
            next_step=(
                "Model bytes are not hash-verified yet; finish the download or import the "
                "offline bundle before approving the first broadcast."
            ),
        )
    )
    return [
        ModelSetupItem(
            name=label,
            status="planned",
            proof_state="proof_unavailable",
            next_step="Review model disk and network requirements before starting setup.",
        ),
        ModelSetupItem(
            name=label,
            status="running",
            proof_state="proof_unavailable",
            next_step="Keep the installer open while model artifacts are downloaded.",
        ),
        ModelSetupItem(
            name=label,
            status="progress",
            proof_state="proof_unavailable",
            next_step="Wait for all model bytes to finish and hash verification to run.",
        ),
        final_state,
    ]


def cancel_model_setup(model: str) -> ModelSetupItem:
    """Represent a cancelled model setup without proof inflation."""

    return ModelSetupItem(
        name=model,
        status="cancelled",
        proof_state="proof_unavailable",
        next_step=f"Rerun model setup for {model} before approving first broadcast.",
    )


def mark_model_skipped(model: str, *, reason: str) -> ModelSetupItem:
    """Represent an operator-skipped model lane."""

    return ModelSetupItem(
        name=model,
        status="skipped",
        proof_state="proof_unavailable",
        next_step=f"{model} was skipped: {reason}. Install or explicitly defer it before release.",
    )


def mark_model_unavailable(model: str, *, reason: str) -> ModelSetupItem:
    """Represent an unavailable model provider lane."""

    return ModelSetupItem(
        name=model,
        status="unavailable",
        proof_state="proof_unavailable",
        next_step=f"{model} is unavailable: {reason}. Restore provider access or import an offline bundle.",
    )


def import_offline_model_bundle(
    *, bundle_dir: Path, expected_hashes: dict[str, str]
) -> ModelSetupResult:
    """Verify an offline model bundle with real file hashes."""

    items: list[ModelSetupItem] = []
    for filename, expected_hash in expected_hashes.items():
        path = bundle_dir / filename
        if not path.exists():
            return ModelSetupResult(
                status="blocked",
                ready=False,
                items=items,
                next_step=f"Copy {filename} into {bundle_dir}, then rerun offline model import.",
            )
        observed_hash = compute_sha256(path)
        if observed_hash != expected_hash:
            return ModelSetupResult(
                status="blocked",
                ready=False,
                items=[
                    *items,
                    ModelSetupItem(
                        name=filename,
                        status="blocked",
                        proof_state="proof_unavailable",
                        sha256=observed_hash,
                        next_step=f"{filename} hash mismatch; rebuild the offline model bundle.",
                    ),
                ],
                next_step=f"{filename} hash mismatch; rebuild the offline model bundle and rerun import.",
            )
        items.append(
            ModelSetupItem(
                name=filename,
                status="complete",
                proof_state="hash_verified",
                sha256=observed_hash,
                next_step=f"{filename} hash verified from offline bundle bytes.",
            )
        )

    return ModelSetupResult(
        status="complete",
        ready=True,
        items=items,
        next_step="Offline model bundle hashes are verified.",
    )
