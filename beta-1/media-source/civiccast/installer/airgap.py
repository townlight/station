# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Air-gapped bundle proof verification."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from civiccast.installer.models import (
    AirGapArtifactProof,
    AirGapProofMetadata,
    AirGapReason,
    AirGapVerificationResult,
)
from civiccast.installer.packages import compute_sha256


def verify_airgap_bundle(
    bundle_dir: Path, *, proof_manifest: Path, network_enabled: bool
) -> AirGapVerificationResult:
    """Verify air-gapped import metadata without permitting network fallback."""

    if network_enabled:
        return _blocked(
            "network_enabled",
            "Disable network access before running air-gapped bundle verification.",
        )
    if not proof_manifest.exists():
        return _blocked(
            "missing_proof_metadata",
            "Rebuild the air-gapped bundle with proof metadata before import.",
        )

    try:
        payload = json.loads(proof_manifest.read_text(encoding="utf-8"))
        artifacts = [
            AirGapArtifactProof.model_validate(item) for item in payload.get("artifacts", [])
        ]
        proof = AirGapProofMetadata(
            artifacts=artifacts,
            network_required=bool(payload.get("network_required", False)),
        )
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        return _blocked(
            "missing_proof_metadata",
            f"Rebuild the air-gapped bundle with valid proof metadata: {exc}.",
        )

    guide = payload.get("operator_guide")
    if not isinstance(guide, str) or not (bundle_dir / guide).exists():
        return _blocked(
            "missing_operator_guide",
            "Add the air-gapped operator guide to the bundle and rerun verification.",
            proof_metadata=proof,
        )
    if proof.network_required:
        return _blocked(
            "network_enabled",
            "Proof metadata requires network access; rebuild the bundle for offline verification.",
            operator_guide=guide,
            proof_metadata=proof,
        )
    if not proof.artifacts:
        return _blocked(
            "missing_proof_metadata",
            "Rebuild the air-gapped bundle with artifact hash proof metadata.",
            operator_guide=guide,
            proof_metadata=proof,
        )

    for artifact in proof.artifacts:
        path = bundle_dir / artifact.filename
        if not path.exists():
            return _blocked(
                "missing_artifact",
                f"Copy {artifact.filename} into the air-gapped bundle and rerun verification.",
                operator_guide=guide,
                proof_metadata=proof,
            )
        observed_hash = compute_sha256(path)
        if observed_hash != artifact.sha256:
            return _blocked(
                "hash_mismatch",
                f"{artifact.filename} hash mismatch; rebuild the air-gapped bundle from verified bytes.",
                operator_guide=guide,
                proof_metadata=proof,
            )

    return AirGapVerificationResult(
        status="ok",
        ready=True,
        reason="verified",
        operator_guide=guide,
        proof_metadata=proof,
        next_step="Air-gapped bundle proof metadata, operator guide, and artifact hashes are verified.",
    )


def verify_external_provider_lane(
    *, provider: str, credentials_present: bool, offline_mode: bool
) -> AirGapVerificationResult:
    """Keep external provider lanes credential-gated in offline installer flows."""

    if credentials_present and not offline_mode:
        return AirGapVerificationResult(
            status="ok",
            ready=True,
            reason="verified",
            operator_guide=None,
            proof_metadata=None,
            next_step=f"{provider} credentials are present for an online verification lane.",
        )
    label = provider.replace("-", " ").title()
    return _blocked(
        "credential_or_secret_required",
        (
            f"Enter {label} credentials only in the approved online proof flow; "
            "keep air-gapped import offline."
        ),
    )


def _blocked(
    reason: AirGapReason,
    next_step: str,
    *,
    operator_guide: str | None = None,
    proof_metadata: AirGapProofMetadata | None = None,
) -> AirGapVerificationResult:
    return AirGapVerificationResult(
        status="blocked",
        ready=False,
        reason=reason,
        operator_guide=operator_guide,
        proof_metadata=proof_metadata,
        next_step=next_step,
    )
