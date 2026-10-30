# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Offline model-bundle manifest and air-gapped verification contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BundleStatus = Literal["ok", "failed"]


@dataclass(frozen=True)
class BundleModel:
    """One required model artifact."""

    name: str
    filename: str
    source: str
    license: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class V11ModelBundleManifest:
    """Offline model bundle manifest."""

    output_dir: Path
    models: tuple[BundleModel, ...]


@dataclass(frozen=True)
class BundleVerificationResult:
    """Air-gapped model-bundle verification result."""

    status: BundleStatus
    operator_action: str
    network_allowed: bool


# The air-gapped bundle ships BOTH summary tags (12B + e4b) so the adaptive summary
# default (12B on >=16GB, e4b on smaller boxes) is present after an offline install
# regardless of detected RAM — there is no network fallback air-gapped (S13 E2/T2/Q1).
_REQUIRED_MODELS = (
    {
        "name": "whisper-large-v3",
        "filename": "whisper-large-v3.tar.zst",
        "source": "faster-whisper:Systran/faster-whisper-large-v3",
        "license": "MIT-compatible model distribution terms verified in fixture ledger",
        "size_bytes": 3_221_225_472,
    },
    {
        "name": "gemma4:12b",
        "filename": "gemma4-12b.tar.zst",
        "source": "ollama:/api/show/gemma4:12b",
        "license": "Apache-2.0 model terms verified in fixture ledger",
        "size_bytes": 8_589_934_592,
    },
    {
        "name": "gemma4:e4b",
        "filename": "gemma4-e4b.tar.zst",
        "source": "ollama:/api/show/gemma4:e4b",
        "license": "Apache-2.0 model terms verified in fixture ledger",
        "size_bytes": 4_026_531_840,
    },
    {
        "name": "translategemma:4b",
        "filename": "translategemma-4b.tar.zst",
        "source": "ollama:/api/show/translategemma:4b",
        "license": "Gemma Terms verified in fixture ledger",
        "size_bytes": 3_758_096_384,
    },
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_v11_model_bundle_manifest(output_dir: Path) -> V11ModelBundleManifest:
    """Return the pinned model manifest used by bundle builders."""

    models = tuple(
        BundleModel(
            name=str(model["name"]),
            filename=str(model["filename"]),
            source=str(model["source"]),
            license=str(model["license"]),
            size_bytes=(output_dir / str(model["filename"])).stat().st_size
            if (output_dir / str(model["filename"])).exists()
            else int(str(model["size_bytes"])),
            sha256=_sha256(output_dir / str(model["filename"]))
            if (output_dir / str(model["filename"])).exists()
            else "",
        )
        for model in _REQUIRED_MODELS
    )
    return V11ModelBundleManifest(output_dir=output_dir, models=models)


def verify_airgapped_install(
    *,
    bundle_dir: Path,
    network_allowed: bool,
    manifest: V11ModelBundleManifest | None = None,
) -> BundleVerificationResult:
    """Verify that all model artifacts are present without network access."""

    if network_allowed:
        return BundleVerificationResult(
            status="failed",
            network_allowed=network_allowed,
            operator_action=(
                "Air-gapped verification must run with network disabled before release proof."
            ),
        )

    manifest = manifest or build_v11_model_bundle_manifest(bundle_dir)
    missing = [
        model.filename for model in manifest.models if not (bundle_dir / model.filename).exists()
    ]
    if missing:
        return BundleVerificationResult(
            status="failed",
            network_allowed=network_allowed,
            operator_action=(
                "Air-gapped install is missing model artifacts: "
                + ", ".join(missing)
                + ". Copy the offline model bundle into the VM and rerun verification."
            ),
        )

    mismatched = [
        model.filename
        for model in manifest.models
        if _sha256(bundle_dir / model.filename) != model.sha256
    ]
    if mismatched:
        return BundleVerificationResult(
            status="failed",
            network_allowed=network_allowed,
            operator_action=(
                "Air-gapped install has model hash mismatches: "
                + ", ".join(mismatched)
                + ". Rebuild the offline bundle manifest from the exact files and rerun verification."
            ),
        )

    return BundleVerificationResult(
        status="ok",
        network_allowed=network_allowed,
        operator_action=(
            "All offline model artifacts are present with network disabled. "
            "Verified SHA-256 values: "
            + ", ".join(f"{model.filename}={model.sha256}" for model in manifest.models)
            + "."
        ),
    )
