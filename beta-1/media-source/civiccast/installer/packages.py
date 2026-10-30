# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Installer package sidecar and attestation verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from civiccast.installer.models import (
    BootstrapMetadata,
    PackageVerificationReason,
    PackageVerificationResult,
    ServiceMetadata,
)


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 from artifact bytes."""

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_package_artifact(artifact: Path, sidecar: Path) -> PackageVerificationResult:
    """Verify an installer artifact against its sidecar and real signing evidence.

    The native release chain signs the Windows installer via Azure Trusted
    Signing (Authenticode) in ``.github/workflows/sign-native-installer.yml``;
    this repo carries no cosign/Sigstore step anywhere, and no code path
    produces a ``*.sigstore.json`` bundle. A sidecar's ``install_manifest.signed``
    flag is therefore never trusted as a bare claim: when it is ``true`` for a
    Windows PE (``.exe``) artifact, this function requires the artifact bytes
    to actually carry an embedded Authenticode certificate table (data
    directory index 4) — the same real, on-disk evidence
    ``scripts/policy/check_sidecar_attestation_integrity.py`` checks. Full
    certificate-chain and timestamp validity remain the CI
    ``Get-AuthenticodeSignature`` fail-closed step's job, not this function's.
    Non-Windows package kinds (``.deb``, ``.rpm``, ``.pkg``, portable
    archives, …) have no code-signing mechanism in this product line today,
    so a ``signed: true`` claim for one of those cannot be independently
    verified and is rejected rather than trusted blind.
    """

    if not artifact.exists():
        return _blocked(
            "missing_artifact",
            f"{artifact.name} is missing; rebuild the package artifact and rerun verification.",
        )
    if not sidecar.exists():
        return _blocked(
            "missing_sidecar",
            f"{sidecar.name} is missing; rebuild the package artifact with its sidecar.",
        )

    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _blocked("invalid_sidecar", f"Read a valid JSON sidecar before release: {exc}.")

    actual_sha = compute_sha256(artifact)
    expected_sha = payload.get("sha256")
    if expected_sha != actual_sha:
        return _blocked(
            "hash_mismatch",
            f"{artifact.name} SHA-256 does not match its sidecar; rebuild from clean bytes.",
            sha256=actual_sha,
        )

    install_manifest = payload.get("install_manifest")
    if not isinstance(install_manifest, dict):
        return _blocked(
            "invalid_sidecar",
            "Install manifest must include service and bootstrap metadata.",
            sha256=actual_sha,
        )

    try:
        service = _service_from_manifest(install_manifest)
        additional_services = _additional_services_from_manifest(install_manifest)
        bootstrap = _bootstrap_from_manifest(install_manifest)
    except (KeyError, TypeError, ValidationError) as exc:
        return _blocked(
            "invalid_sidecar",
            f"Install manifest must include service and bootstrap metadata: {exc}.",
            sha256=actual_sha,
        )

    claims_signed = install_manifest.get("signed") is True
    attestation: str | None = None
    if claims_signed:
        if artifact.suffix.lower() != ".exe":
            return _blocked(
                "unsigned_artifact",
                f"{artifact.name} sidecar claims signed=true, but this product line "
                "has no code-signing mechanism for non-Windows package kinds; "
                "rebuild the sidecar with signed=false or provide the artifact's "
                "real Windows Authenticode-signed .exe.",
                sha256=actual_sha,
            )
        if not _pe_has_authenticode_evidence(artifact):
            return _blocked(
                "unsigned_artifact",
                f"{artifact.name} sidecar claims signed=true but carries no embedded "
                "Authenticode certificate table; re-sign it via Azure Trusted Signing "
                "(see CODE_SIGNING_POLICY.md) and rebuild the sidecar from the signed bytes.",
                sha256=actual_sha,
            )
        attestation = "authenticode"

    return PackageVerificationResult(
        status="ok",
        ready=True,
        reason="verified",
        sha256=actual_sha,
        service_metadata=service,
        additional_services=additional_services,
        bootstrap_metadata=bootstrap,
        attestation=attestation,
        next_step=(
            "Package bytes match the sidecar hash"
            + (" and the embedded Authenticode certificate table is present" if attestation else "")
            + "; service metadata is valid."
        ),
    )


def _pe_has_authenticode_evidence(path: Path) -> bool:
    """True if a PE file carries embedded Authenticode evidence (a non-empty
    Certificate Table, data directory index 4). Reads the real bytes so the
    recorded signing state cannot drift from the artifact — never a flag.
    Full chain/timestamp validity is enforced separately by the CI
    ``Get-AuthenticodeSignature`` fail-closed step (see
    ``.github/workflows/sign-native-installer.yml``). Duplicated (rather than
    imported) from ``scripts/policy/check_sidecar_attestation_integrity.py``
    so this runtime module never depends on the policy script tree.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if len(data) < 0x40 or data[:2] != b"MZ":
        return False
    e_lfanew = int.from_bytes(data[0x3C:0x40], "little")
    if len(data) < e_lfanew + 24 or data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return False
    opt = e_lfanew + 24
    magic = int.from_bytes(data[opt : opt + 2], "little")
    if magic == 0x10B:  # PE32
        dd_start = opt + 96
    elif magic == 0x20B:  # PE32+
        dd_start = opt + 112
    else:
        return False
    cert_entry = dd_start + 4 * 8  # data directory index 4 = Certificate Table
    if len(data) < cert_entry + 8:
        return False
    cert_offset = int.from_bytes(data[cert_entry : cert_entry + 4], "little")
    cert_size = int.from_bytes(data[cert_entry + 4 : cert_entry + 8], "little")
    return cert_offset > 0 and cert_size > 0 and cert_offset + cert_size <= len(data)


def _service_from_manifest(install_manifest: dict[str, Any]) -> ServiceMetadata:
    raw = install_manifest["service"]
    return _service_metadata_from_raw(raw)


def _additional_services_from_manifest(install_manifest: dict[str, Any]) -> list[ServiceMetadata]:
    raw_services = install_manifest.get("additional_services", [])
    if not isinstance(raw_services, list):
        raise TypeError("additional_services must be a list when present")
    services: list[ServiceMetadata] = []
    for raw in raw_services:
        if not isinstance(raw, dict):
            raise TypeError("additional_services entries must be objects")
        services.append(_service_metadata_from_raw(raw))
    return services


def _service_metadata_from_raw(raw: dict[str, Any]) -> ServiceMetadata:
    return ServiceMetadata(
        manager=str(raw["manager"]),
        name=str(raw.get("name") or raw.get("service_name") or "civiccast"),
        service_name=str(raw.get("service_name") or raw.get("name") or "civiccast"),
        host_service=bool(raw.get("host_service", False)),
        restart_policy=raw.get("restart_policy"),
        recovery_window_seconds=raw.get("recovery_window_seconds"),
    )


def _bootstrap_from_manifest(install_manifest: dict[str, Any]) -> BootstrapMetadata:
    raw = install_manifest["bootstrap"]
    return BootstrapMetadata(
        package_kind=str(raw["package_kind"]),
        package_manager=raw.get("package_manager"),
    )


def _blocked(
    reason: PackageVerificationReason,
    next_step: str,
    *,
    sha256: str | None = None,
) -> PackageVerificationResult:
    return PackageVerificationResult(
        status="blocked",
        ready=False,
        reason=reason,
        sha256=sha256,
        service_metadata=None,
        bootstrap_metadata=None,
        attestation=None,
        next_step=next_step,
    )
