# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Beta tester handoff readiness composition."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from civiccast import __version__
from civiccast.cable.ndi import check_ndi_runtime
from civiccast.installer.model_state import mark_model_unavailable
from civiccast.installer.models import (
    BetaHandoffArtifact,
    BetaHandoffLane,
    BetaHandoffStatus,
    BetaHandoffSummary,
)
from civiccast.stream._ffmpeg import check_ffmpeg

_DEFAULT_MANIFEST = Path("artifacts/release") / (
    f"civiccast-{__version__}-release-artifacts-manifest.json"
)
_DEFAULT_CLEAN_PROOF = Path("docs/releases/evidence/v1.2-clean-windows-install-proof.json")
_RUN_CLEAN_PROOF = (
    Path(".agent-runs")
    / "2026-05-21-beta-tester-handoff"
    / "evidence"
    / "clean-windows-install.json"
)


def build_beta_handoff_summary(
    *,
    release_manifest: Path | None = None,
    clean_windows_evidence: Path | None = None,
) -> BetaHandoffSummary:
    """Build a fail-closed beta tester handoff summary.

    This function reports only evidence metadata and environment presence. It
    never returns configured credential values.
    """

    manifest_path = release_manifest or _DEFAULT_MANIFEST
    clean_proof_path = clean_windows_evidence or _first_existing(
        _RUN_CLEAN_PROOF,
        _DEFAULT_CLEAN_PROOF,
    )
    manifest = _read_json(manifest_path)
    acquisition = manifest.get("beta_handoff_acquisition", {}) if manifest else {}
    artifacts = _artifacts_from_acquisition(acquisition)
    install_command = acquisition.get("install_command")
    lanes = [
        _package_lane(manifest_path, acquisition),
        _clean_windows_lane(clean_proof_path),
        _dependency_lane(),
        _model_lane(),
        _mtls_lane(),
        _activitypub_lane(),
        _external_provider_lane(),
    ]
    return BetaHandoffSummary(
        ready=all(lane.ready for lane in lanes),
        version=str(manifest.get("version") or __version__),
        acquisition_manifest=str(manifest_path) if manifest_path.exists() else None,
        install_command=install_command if isinstance(install_command, str) else None,
        artifacts=artifacts,
        lanes=lanes,
    )


def _first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifacts_from_acquisition(acquisition: object) -> list[BetaHandoffArtifact]:
    if not isinstance(acquisition, dict):
        return []
    hashes = acquisition.get("hashes")
    hash_map = hashes if isinstance(hashes, dict) else {}
    artifacts: list[BetaHandoffArtifact] = []
    for key in (
        "windows_installer",
        "wheel",
        "wheelhouse",
        "model_bundle_manifest",
    ):
        item = acquisition.get(key)
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if isinstance(filename, str) and filename:
            digest = hash_map.get(key)
            artifacts.append(
                BetaHandoffArtifact(
                    filename=filename,
                    sha256=digest if isinstance(digest, str) and digest else None,
                )
            )
    return artifacts


def _lane(
    *,
    lane_id: str,
    label: str,
    status: BetaHandoffStatus,
    message: str,
    operator_action: str,
    evidence_target: str,
) -> BetaHandoffLane:
    return BetaHandoffLane(
        id=lane_id,  # type: ignore[arg-type]
        label=label,
        status=status,
        ready=status == "passed",
        message=message,
        operator_action=operator_action,
        evidence_target=evidence_target,
    )


def _package_lane(manifest_path: Path, acquisition: object) -> BetaHandoffLane:
    if isinstance(acquisition, dict) and _acquisition_complete(acquisition):
        return _lane(
            lane_id="package-acquisition",
            label="Package acquisition",
            status="passed",
            message="Release manifest ties installer, wheel, wheelhouse, model manifest, hashes, and install command together.",
            operator_action="Use the release manifest artifact set and verify hashes before first-run setup.",
            evidence_target=str(manifest_path),
        )
    return _lane(
        lane_id="package-acquisition",
        label="Package acquisition",
        status="blocked",
        message="Beta package acquisition is blocked until the release artifact manifest contains the handoff contract.",
        operator_action="Populate the release artifact manifest's beta_handoff_acquisition contract (installer, wheel, wheelhouse, model manifest, hashes, install command) before handing artifacts to testers.",
        evidence_target=str(manifest_path),
    )


def _acquisition_complete(acquisition: dict[str, Any]) -> bool:
    hashes = acquisition.get("hashes")
    required = (
        "windows_installer",
        "wheel",
        "wheelhouse",
        "model_bundle_manifest",
    )
    if not isinstance(hashes, dict):
        return False
    if not all(isinstance(acquisition.get(key), dict) for key in required):
        return False
    if not all(isinstance(hashes.get(key), str) and hashes[key] for key in required):
        return False
    return isinstance(acquisition.get("install_command"), str) and bool(
        acquisition["install_command"]
    )


def _clean_windows_lane(evidence_path: Path) -> BetaHandoffLane:
    payload = _read_json(evidence_path)
    status = payload.get("status")
    if status in {"passed", "partial", "blocked"}:
        lane_status: BetaHandoffStatus = "passed" if status == "passed" else "blocked"
        partial = status == "partial"
        return _lane(
            lane_id="clean-windows-install-proof",
            label="Clean Windows install proof",
            status=lane_status,
            message=(
                "Clean Windows install proof evidence is recorded."
                if lane_status == "passed"
                else "Runtime-only WSL2 proof is recorded, but native isolated Windows installer proof is still required."
                if partial
                else "Clean Windows install proof is blocked by recorded host capability evidence."
            ),
            operator_action=(
                "Retain the clean-machine transcript with the release evidence."
                if lane_status == "passed"
                else "Rerun on an isolated Windows target and retain the installer-to-dashboard transcript."
                if partial
                else "Resolve the recorded host blocker or rerun on an isolated Windows target."
            ),
            evidence_target=str(evidence_path),
        )
    return _lane(
        lane_id="clean-windows-install-proof",
        label="Clean Windows install proof",
        status="blocked",
        message="No clean Windows install proof evidence has been recorded for this beta handoff.",
        operator_action="Run scripts/run_clean_windows_install_proof.py --execute and retain the JSON and Markdown evidence.",
        evidence_target=str(evidence_path),
    )


def _dependency_lane() -> BetaHandoffLane:
    ffmpeg = check_ffmpeg()
    ndi = check_ndi_runtime()
    ollama_present = shutil.which("ollama") is not None
    if ffmpeg and ndi.status == "ok" and ollama_present:
        return _lane(
            lane_id="dependencies",
            label="Local dependencies",
            status="passed",
            message="FFmpeg, NDI runtime, and Ollama were detected locally.",
            operator_action="Keep these runtimes installed before first broadcast.",
            evidence_target="civiccast installer beta-handoff",
        )
    missing = []
    if not ffmpeg:
        missing.append("FFmpeg")
    if ndi.status != "ok":
        missing.append("NDI runtime or sender")
    if not ollama_present:
        missing.append("Ollama")
    status: BetaHandoffStatus = "hardware_required" if ndi.status != "ok" else "blocked"
    return _lane(
        lane_id="dependencies",
        label="Local dependencies",
        status=status,
        message=f"Dependency proof is incomplete for: {', '.join(missing)}.",
        operator_action="Install or operator-gate missing runtimes, then rerun installer beta-handoff.",
        evidence_target="civiccast installer beta-handoff",
    )


def _model_lane() -> BetaHandoffLane:
    model_item = mark_model_unavailable("gemma4:e4b", reason="provider proof not recorded")
    return _lane(
        lane_id="models",
        label="AI model proof",
        status="blocked",
        message="Caption, summary, and translation models are not marked ready without hash proof.",
        operator_action=model_item.next_step,
        evidence_target="docs/releases/evidence/v1.2-first-run-gates.md",
    )


def _mtls_lane() -> BetaHandoffLane:
    """Local CA mTLS readiness lane.

    NATS JetStream was removed from the product (owner decision 2026-08-20;
    see ADR 0023, which supersedes ADR 0001) -- this lane used to run
    alongside a paired ``_nats_lane`` (deleted); local-CA mTLS readiness
    stands on its own now, covering only the ``civiccast-api`` and
    ``civiccast-worker`` service identities.
    """

    try:
        from civiccast.certs import readiness

        ready = readiness.check_mtls_readiness()
    except Exception as exc:
        return _lane(
            lane_id="mtls",
            label="Local CA mTLS",
            status="blocked",
            message=f"Local CA mTLS readiness is blocked: {exc}",
            operator_action="Run civiccast cert rotate for civiccast-api and civiccast-worker.",
            evidence_target="docs/ops/local-ca-mtls.md",
        )
    if ready is True:
        return _lane(
            lane_id="mtls",
            label="Local CA mTLS",
            status="passed",
            message="Local CA and service certificate readiness returned a positive proof.",
            operator_action="Retain certificate rotation evidence and rotate on schedule.",
            evidence_target="docs/ops/local-ca-mtls.md",
        )
    return _lane(
        lane_id="mtls",
        label="Local CA mTLS",
        status="blocked",
        message="Local CA mTLS readiness did not return a positive proof.",
        operator_action="Rotate required service certificates and rerun beta-handoff.",
        evidence_target="docs/ops/local-ca-mtls.md",
    )


def _activitypub_lane() -> BetaHandoffLane:
    from civiccast.activitypub.config import load_activitypub_config

    config = load_activitypub_config()
    if config.federation_mode == "disabled":
        return _lane(
            lane_id="activitypub",
            label="ActivityPub federation",
            status="passed",
            message="Federation is disabled by default; no public ActivityPub actor is exposed.",
            operator_action="Leave this optional feature disabled for normal installation, or ask a technical administrator to follow the advanced federation guide after CivicCast is running.",
            evidence_target="docs/ops/activitypub-federation.md",
        )
    if config.base_url and config.private_key_path and config.public_key_pem:
        return _lane(
            lane_id="activitypub",
            label="ActivityPub federation",
            status="passed",
            message=f"ActivityPub is configured in {config.federation_mode} mode with station key material and explicit public base URL.",
            operator_action="Retain key-generation evidence, run the signed federation smoke test, and approve/reject follows from the operator console Federation screen.",
            evidence_target="docs/ops/activitypub-federation.md",
        )
    return _lane(
        lane_id="activitypub",
        label="ActivityPub federation",
        status="blocked",
        message="ActivityPub mode was requested but key material or public base URL is incomplete.",
        operator_action="Keep federation unavailable and ask a technical administrator to complete the advanced federation guide, then restart CivicCast and rerun this handoff check.",
        evidence_target="docs/ops/activitypub-federation.md",
    )


def _external_provider_lane() -> BetaHandoffLane:
    configured = [
        name
        for name in (
            "CIVICCAST_INTERNET_ARCHIVE_ACCESS_KEY",
            "CIVICCAST_YOUTUBE_CLIENT_SECRET",
            "CIVICCAST_SUBSCRIBER_WEBHOOK_SECRET",
        )
        if os.getenv(name)
    ]
    if configured:
        message = "External provider credentials are present, but controlled live proof is not recorded in this handoff."
    else:
        message = "External provider proof requires approved credentials or controlled targets."
    return _lane(
        lane_id="external-providers",
        label="External provider proof",
        status="credential_or_secret_required",
        message=message,
        operator_action="Run controlled Internet Archive, YouTube, webhook, email, and podcast proof only with approved credentials and redacted evidence.",
        evidence_target="docs/ops/credential-matrix.md",
    )
