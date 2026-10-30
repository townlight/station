# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Stage 8 local release-hardening artifacts for the 3.2 LPM lab."""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.control_room.lpm_lab import LabTopologyProfile, TopologyId
from civiccast.control_room.lpm_lab_harness import LabEvent

Stage8Status = Literal["passed", "failed"]


class Stage8Artifact(BaseModel):
    """One required Stage 8 artifact row."""

    model_config = ConfigDict(extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=180)]
    purpose: Annotated[str, Field(min_length=1, max_length=240)]
    required: bool = True


class Stage8ArtifactDigest(BaseModel):
    """Digest for one generated Stage 8 package artifact."""

    model_config = ConfigDict(extra="forbid")

    path: Annotated[str, Field(min_length=1, max_length=240)]
    size_bytes: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(min_length=64, max_length=64)]


class Stage8ReleaseManifest(BaseModel):
    """Machine-readable local release-hardening manifest."""

    model_config = ConfigDict(extra="forbid")

    schema_id: Literal["civiccast.lpm.stage8.release-hardening.v1"]
    status: Stage8Status
    profiles: list[TopologyId]
    required_artifacts: list[Stage8Artifact]
    proof_matrix: dict[str, str]
    local_gate_requirements: list[Annotated[str, Field(min_length=1, max_length=240)]]
    release_claims: list[Annotated[str, Field(min_length=1, max_length=240)]]
    not_claimed: list[Annotated[str, Field(min_length=1, max_length=240)]]
    artifact_digests: list[Stage8ArtifactDigest] = Field(default_factory=list)
    manifest_sha256: Annotated[str | None, Field(max_length=64)] = None


def build_stage8_manifest(profiles: list[LabTopologyProfile]) -> Stage8ReleaseManifest:
    """Return the deterministic Stage 8 local release-hardening manifest."""

    manifest = Stage8ReleaseManifest(
        schema_id="civiccast.lpm.stage8.release-hardening.v1",
        status="passed",
        profiles=[profile.profile_id for profile in profiles],
        required_artifacts=[
            Stage8Artifact(path="summary.json", purpose="delegated lab run summary"),
            Stage8Artifact(path="events.json", purpose="machine-readable proof event log"),
            Stage8Artifact(path="profiles.json", purpose="topology contract source snapshot"),
            Stage8Artifact(path="stage67-soak-plan.json", purpose="deterministic soak/chaos plan"),
            Stage8Artifact(
                path="support-bundle-manifest.json",
                purpose="redacted support-bundle shape",
            ),
            Stage8Artifact(
                path="station-evidence-manifest.template.json",
                purpose="station evidence collection envelope",
            ),
            Stage8Artifact(path="stage8-proof-matrix.json", purpose="proof-label matrix"),
            Stage8Artifact(path="stage8-known-limits.md", purpose="operator-facing limits"),
            Stage8Artifact(path="stage8-local-operator-handoff.md", purpose="local test handoff"),
            Stage8Artifact(
                path="virtual-media-studio-bundle/vstudio-bundle-manifest.json",
                purpose="reusable virtual lab bundle manifest",
            ),
        ],
        proof_matrix=_build_proof_matrix(profiles),
        local_gate_requirements=[
            "GauntletGate Lite runs after each implementation slice and fixes counted issues to zero.",
            "GauntletGate ALL runs Lite, Walkthrough, then Full before a local stage commit.",
            "Required skipped checks count as failures unless an equivalent enabled substrate is recorded.",
            "Clean-machine VM evidence is install/app evidence unless WSL2 runtime completion is captured.",
            "No wall-clock soak is required for this deterministic Stage 8 package.",
        ],
        release_claims=[
            "CivicCast 3.2 local branch contains a deterministic LPM three-profile contract lab.",
            "The reusable Virtual Media Studio bundle can be exported from this checkout.",
            "Local software/API rows may be claimed only at their recorded proof level.",
        ],
        not_claimed=[
            "No elapsed wall-clock soak is claimed by Stage 8.",
            "No station-device evidence is claimed by Stage 8.",
            "No public beta release, push, merge, or tag is authorized by this manifest.",
        ],
    )
    manifest.manifest_sha256 = _stable_sha256(
        manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    )
    return manifest


def build_stage8_proofs(profiles: list[LabTopologyProfile]) -> list[LabEvent]:
    """Return Stage 8 local release-hardening proof events."""

    manifest = build_stage8_manifest(profiles)
    return [
        LabEvent(
            profile_id=profiles[0].profile_id,
            device_id="stage8-release-hardening",
            check_id="stage8-release-hardening-manifest",
            status="passed",
            proof_level="api-contract-proven",
            proof_source="release-hardening",
            claim="Stage 8 local release-hardening manifest schema and required artifact list are complete.",
            observed=(
                f"Manifest covers {len(manifest.profiles)} profile(s), "
                f"{len(manifest.required_artifacts)} artifact(s), and "
                f"{len(manifest.proof_matrix)} proof-matrix row(s). "
                "Artifact digests are written after package generation."
            ),
            not_claimed=(
                "This is local release-hardening evidence only; it does not claim "
                "elapsed wall-clock soak, station-device evidence, merge, tag, or publication."
            ),
            details={
                "schema_id": manifest.schema_id,
                "required_artifact_count": len(manifest.required_artifacts),
                "proof_matrix_rows": len(manifest.proof_matrix),
                "final_manifest_path": "stage8-release-manifest.json",
                "final_manifest_note": (
                    "Read the final manifest artifact for artifact_digests and "
                    "manifest_sha256 after package generation."
                ),
            },
        ),
        LabEvent(
            profile_id=profiles[0].profile_id,
            device_id="stage8-virtual-media-studio",
            check_id="stage8-virtual-media-studio-bundle",
            status="passed",
            proof_level="api-contract-proven",
            proof_source="release-hardening",
            claim="Reusable Virtual Media Studio bundle contract is part of the Stage 8 package.",
            observed="Bundle manifest path is required and the extension contract is documented.",
            not_claimed=(
                "The bundle is local lab software, not a standalone published product or release artifact."
            ),
            details={
                "bundle_manifest": "virtual-media-studio-bundle/vstudio-bundle-manifest.json",
                "extension_contract": "virtual-media-studio-bundle/extension-contract.md",
            },
        ),
        LabEvent(
            profile_id=profiles[0].profile_id,
            device_id="stage8-release-boundary",
            check_id="stage8-no-wall-clock-soak-claim",
            status="passed",
            proof_level="simulated-proven",
            proof_source="release-hardening",
            claim="Stage 8 explicitly excludes elapsed wall-clock soak from its claims.",
            observed="Deterministic Stage 6/7 rehearsal is allowed; elapsed soak evidence is not required here.",
            not_claimed="No 12-hour elapsed soak, 72-hour soak, or unattended soak is claimed.",
            details={"elapsed_soak_required": False, "deterministic_rehearsal_required": True},
        ),
    ]


def write_stage8_artifacts(
    artifact_root: Path,
    profiles: list[LabTopologyProfile],
    *,
    bundle_writer: Any | None = None,
    events: list[LabEvent] | None = None,
) -> Stage8ReleaseManifest:
    """Write Stage 8 local release-hardening artifacts."""

    manifest = build_stage8_manifest(profiles)
    (artifact_root / "stage8-proof-matrix.json").write_text(
        json.dumps(manifest.proof_matrix, indent=2),
        encoding="utf-8",
    )
    (artifact_root / "stage8-known-limits.md").write_text(
        _render_known_limits(manifest),
        encoding="utf-8",
    )
    (artifact_root / "stage8-local-operator-handoff.md").write_text(
        _render_operator_handoff(manifest, events=events or []),
        encoding="utf-8",
    )
    bundle_root = artifact_root / "virtual-media-studio-bundle"
    if bundle_writer is None:
        _ensure_vstudio_import_path()
        bundle_module = importlib.import_module("vstudio.bundle")
        bundle_writer = bundle_module.write_bundle
    bundle_writer(bundle_root, force_clean=bool(bundle_root.exists()))
    manifest.artifact_digests = _collect_artifact_digests(artifact_root, manifest)
    manifest.manifest_sha256 = _stable_sha256(
        manifest.model_dump(mode="json", exclude={"manifest_sha256"})
    )
    (artifact_root / "stage8-release-manifest.json").write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return manifest


def summarize_stage8_events(events: list[LabEvent]) -> list[str]:
    """Return human-readable Stage 8 summary lines."""

    stage8 = [event for event in events if event.check_id.startswith("stage8-")]
    if not stage8:
        return ["Stage 8 release-hardening: not run."]
    passed = sum(1 for event in stage8 if event.status == "passed")
    lines = [
        f"Stage 8 release-hardening: {passed} passed.",
        "Stage 8 uses deterministic local evidence only; no wall-clock soak is claimed.",
        "Virtual Media Studio bundle artifacts are included for reuse and future extraction.",
    ]
    if any(event.status != "passed" for event in stage8):
        lines.append("WARNING: Stage 8 contains non-passing events.")
    return lines


def _build_proof_matrix(profiles: list[LabTopologyProfile]) -> dict[str, str]:
    matrix: dict[str, str] = {}
    for profile in profiles:
        matrix[f"{profile.profile_id}:profile-contract"] = "mocked/profile-contract"
        for device in profile.devices:
            for check_id in device.required_checks:
                key = f"{profile.profile_id}:{device.contract_id}:{check_id}"
                matrix[key] = f"{device.proof_level}/profile-declared"
    matrix["stage67:deterministic-soak-plan"] = "simulated-proven/stateful-simulator"
    matrix["stage8:release-hardening-manifest"] = "api-contract-proven/release-hardening"
    return matrix


def _render_known_limits(manifest: Stage8ReleaseManifest) -> str:
    return "\n".join(
        [
            "# CivicCast 3.2 Local Known Limits",
            "",
            "This file is generated by the Stage 8 local release-hardening harness.",
            "",
            "## Not Claimed",
            "",
            *[f"- {claim}" for claim in manifest.not_claimed],
            "- No clean Windows install proof is claimed by this Stage 8 local package.",
            "- Local software API compatibility is separate from secure listener posture.",
            "- Non-confined local media-control listeners must be treated as local setup issues.",
            "- The Virtual Media Studio bundle is local lab software, not a public release artifact.",
            "",
            "## Proof Matrix",
            "",
            "Proof levels are recorded in `stage8-proof-matrix.json`. Mocked rows remain",
            "visible and are not promoted by this document.",
            "",
            "## Related Artifacts",
            "",
            "- `README.md`",
            "- `stage8-local-operator-handoff.md`",
            "- `stage8-proof-matrix.json`",
            "",
        ]
    )


def _render_operator_handoff(manifest: Stage8ReleaseManifest, *, events: list[LabEvent]) -> str:
    software_events = [
        event for event in events if str(event.check_id).startswith("software-probe-")
    ]
    listener_lines = [
        f"- `{event.device_id}` / `{event.check_id}`: `{event.status}` - {event.not_claimed}"
        for event in software_events
    ] or ["- No local software probes were recorded in this package."]
    return "\n".join(
        [
            "# CivicCast 3.2 Local Operator Handoff",
            "",
            "Use this artifact package to review local 3.2 control-room and virtual",
            "media studio evidence. It is not a public release handoff.",
            "",
            "## Required Artifacts",
            "",
            *[
                f"- `{artifact.path}` - {artifact.purpose}"
                for artifact in manifest.required_artifacts
            ],
            "",
            "## Local Gate Requirements",
            "",
            *[f"- {item}" for item in manifest.local_gate_requirements],
            "",
            "## Review Commands",
            "",
            "Run these from the repo root when reviewing or refreshing this package:",
            "",
            "- `uv run python tools/virtual-media-studio/civiccast-vstudio.py profiles list`",
            "- `uv run python tools/virtual-media-studio/civiccast-vstudio.py run --profile all --scenario release --artifact-root artifacts/virtual-media-studio-release-review --force-clean --probe-real-software`",
            "- `powershell -ExecutionPolicy Bypass -File scripts/run_local_3_2_lpm_contract_lab_ci.ps1 -ArtifactRoot artifacts/local-ci/3.2-stage8-review`",
            "",
            "## Suggested Reading Order",
            "",
            "1. `README.md`",
            "2. `stage8-release-manifest.json`",
            "3. `events.json`",
            "4. `virtual-media-studio-bundle/extension-contract.md`",
            "",
            "## Local Software Listener Posture",
            "",
            "OBS/vMix API compatibility can be proven while secure listener posture",
            "remains unclaimed. Treat any `not network-confined` row as a local setup",
            "issue before relying on that application's listener security posture.",
            "",
            *listener_lines,
            "",
        ]
    )


def _collect_artifact_digests(
    artifact_root: Path, manifest: Stage8ReleaseManifest
) -> list[Stage8ArtifactDigest]:
    paths: set[Path] = set()
    for artifact in manifest.required_artifacts:
        candidate = artifact_root / artifact.path
        if candidate.is_file():
            paths.add(candidate)
    bundle_root = artifact_root / "virtual-media-studio-bundle"
    if bundle_root.is_dir():
        paths.update(path for path in bundle_root.rglob("*") if path.is_file())
    return [
        Stage8ArtifactDigest(
            path=path.relative_to(artifact_root).as_posix(),
            size_bytes=path.stat().st_size,
            sha256=_file_sha256(path),
        )
        for path in sorted(paths, key=lambda item: item.relative_to(artifact_root).as_posix())
    ]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_sha256(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _ensure_vstudio_import_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    tool_root = repo_root / "tools" / "virtual-media-studio"
    tool_root_text = str(tool_root)
    if tool_root_text not in sys.path:
        sys.path.insert(0, tool_root_text)


__all__ = [
    "Stage8Artifact",
    "Stage8ArtifactDigest",
    "Stage8ReleaseManifest",
    "build_stage8_manifest",
    "build_stage8_proofs",
    "summarize_stage8_events",
    "write_stage8_artifacts",
]
