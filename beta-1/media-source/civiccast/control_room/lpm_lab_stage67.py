# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Stage 6-7 LPM lab soak, chaos, and station-readiness proofs.

Stage 6 is a deterministic local rehearsal of the three LPM profiles over a
12-hour activity plan. It validates channel coverage, planned fault injection,
recovery expectations, and support-bundle contents without sleeping for 12
hours. Stage 7 validates the station-evidence envelope needed at LPM. It never
claims station-device evidence unless a real station evidence bundle is supplied.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.control_room.lpm_lab import LabTopologyProfile, TopologyId
from civiccast.control_room.lpm_lab_harness import LabEvent

Stage67Status = Literal["passed", "failed", "not-applicable"]
MIN_SOAK_SECONDS = 12 * 60 * 60
FORBIDDEN_SECRET_FRAGMENTS = ("admin/admin", "password=", "secret=", "token=")
DEVICE_EVIDENCE_FIELDS = (
    "device_id",
    "proof_type",
    "observed_state",
    "artifact_path",
    "sha256",
    "captured_at",
)
MEDIA_EVIDENCE_FIELDS = (
    "media_id",
    "proof_type",
    "duration_seconds",
    "artifact_path",
    "sha256",
    "captured_at",
)
KNOWN_PROFILE_IDS = {
    "fixed-studio-livestreaming",
    "portable-field-kit",
    "digitization-obs",
}


class SoakFault(BaseModel):
    """One deterministic fault injected into a channel rehearsal."""

    model_config = ConfigDict(extra="forbid")

    fault_id: Annotated[str, Field(min_length=1, max_length=120)]
    device_class: Annotated[str, Field(min_length=1, max_length=80)]
    inject_at_second: Annotated[int, Field(ge=0)]
    recover_at_second: Annotated[int, Field(ge=1)]
    expected_effect: Annotated[str, Field(min_length=1, max_length=240)]
    recovery_proof: Annotated[str, Field(min_length=1, max_length=240)]


class SoakChannel(BaseModel):
    """One LPM profile mapped into the Stage 6 three-channel rehearsal."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    profile_id: TopologyId
    source_device_id: Annotated[str, Field(min_length=1, max_length=120)]
    expected_outputs: list[Annotated[str, Field(min_length=1, max_length=160)]]
    faults: list[SoakFault]


class SoakPlan(BaseModel):
    """A deterministic long-run plan that can be validated without wall-clock delay."""

    model_config = ConfigDict(extra="forbid")

    plan_id: Annotated[str, Field(min_length=1, max_length=120)]
    duration_seconds: Annotated[int, Field(ge=MIN_SOAK_SECONDS)]
    channels: list[SoakChannel]
    support_bundle_files: list[Annotated[str, Field(min_length=1, max_length=180)]]


class Stage67Proof(BaseModel):
    """One Stage 6-7 proof event before conversion into the lab event schema."""

    model_config = ConfigDict(extra="forbid")

    profile_id: TopologyId
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    check_id: Annotated[str, Field(min_length=1, max_length=120)]
    status: Stage67Status
    proof_source: Literal["stateful-simulator", "station-readiness"]
    proof_level: Literal["mocked", "simulated-proven"]
    claim: Annotated[str, Field(min_length=1, max_length=300)]
    observed: Annotated[str, Field(min_length=1, max_length=600)]
    not_claimed: Annotated[str | None, Field(max_length=600)] = (
        "This is local Stage 6-7 readiness evidence only; it is not station-device evidence."
    )
    details: dict[str, Any] = Field(default_factory=dict)


def build_stage67_proofs(profiles: list[LabTopologyProfile]) -> list[LabEvent]:
    """Return Stage 6-7 local proof events for the selected profiles."""

    plan = build_stage67_soak_plan(profiles)
    plan_summary = validate_soak_plan(plan)
    support_bundle = build_support_bundle_manifest(profiles, plan)
    field_template = build_field_evidence_template(profiles, support_bundle)
    selected_profile_ids = [profile.profile_id for profile in profiles]
    field_template_issues = validate_field_evidence_bundle(
        field_template,
        expected_profile_ids=selected_profile_ids,
        expected_support_manifest_sha256=support_bundle["manifest_sha256"],
    )

    proofs: list[Stage67Proof] = [
        Stage67Proof(
            profile_id=plan.channels[0].profile_id,
            device_id="stage67-soak-orchestrator",
            check_id="stage67-three-channel-soak-plan",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim=_soak_plan_claim(plan),
            observed=(
                f"Validated {plan_summary['channel_count']} channel(s), "
                f"{plan_summary['fault_count']} injected fault(s), and "
                f"{plan_summary['duration_seconds']} planned seconds without wall-clock delay."
            ),
            not_claimed=(
                "This validates a deterministic 12-hour plan; it is not a completed "
                "wall-clock soak, clean Windows install proof, or station-device evidence."
            ),
            details=plan.model_dump(mode="json"),
        ),
        Stage67Proof(
            profile_id=plan.channels[0].profile_id,
            device_id="stage67-support-bundle",
            check_id="stage67-support-bundle-redaction",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="Support bundle manifest includes required proof files and excludes secrets.",
            observed=(
                f"Support bundle manifest contains {len(support_bundle['files'])} file "
                "entries and passed secret-fragment scan."
            ),
            not_claimed=(
                "This is a local manifest proof only; it is not a station-generated "
                "support bundle from LPM."
            ),
            details=support_bundle,
        ),
        Stage67Proof(
            profile_id=plan.channels[0].profile_id,
            device_id="stage67-station-evidence",
            check_id="stage67-station-evidence-envelope",
            status="passed",
            proof_source="station-readiness",
            proof_level="mocked",
            claim="Station-device evidence envelope is defined and blocks incomplete station evidence.",
            observed=(
                "Local template validation intentionally rejects incomplete station evidence: "
                + "; ".join(field_template_issues[:3])
            ),
            not_claimed=(
                "No LPM device was touched and no station-device evidence was supplied; "
                "no station-device label is claimed."
            ),
            details={"template": field_template, "expected_issues": field_template_issues},
        ),
    ]

    for channel in plan.channels:
        proofs.append(
            Stage67Proof(
                profile_id=channel.profile_id,
                device_id=channel.source_device_id,
                check_id="stage67-channel-fault-recovery",
                status="passed",
                proof_source="stateful-simulator",
                proof_level="simulated-proven",
                claim=f"{channel.profile_id} channel has dropout/recovery coverage.",
                observed=(
                    f"{channel.channel_id} covers {len(channel.faults)} fault(s) "
                    f"and {len(channel.expected_outputs)} expected output(s)."
                ),
                not_claimed=(
                    "This is deterministic local fault rehearsal, not a live LPM "
                    "device, media-output, or wall-clock soak result."
                ),
                details=channel.model_dump(mode="json"),
            )
        )

    return [_event_from_stage67_proof(proof) for proof in proofs]


def build_stage67_soak_plan(profiles: list[LabTopologyProfile]) -> SoakPlan:
    """Build the deterministic Stage 6 soak/chaos plan for selected profiles."""

    channels = [_channel_for_profile(profile) for profile in profiles]
    return SoakPlan(
        plan_id="lpm-stage67-12h-three-profile-rehearsal",
        duration_seconds=MIN_SOAK_SECONDS,
        channels=channels,
        support_bundle_files=[
            "summary.json",
            "events.json",
            "profiles.json",
            "README.md",
            "stage67-soak-plan.json",
            "station-evidence-manifest.template.json",
            "support-bundle-manifest.json",
            "adapter-logs/redacted-device-control.log",
            "proof-log/redacted-control-room-actions.jsonl",
        ],
    )


def validate_soak_plan(plan: SoakPlan) -> dict[str, int]:
    """Validate Stage 6 soak plan invariants and return a compact summary."""

    if plan.duration_seconds < MIN_SOAK_SECONDS:
        raise ValueError("Stage 6 soak plan must cover at least 12 hours.")
    if not plan.channels:
        raise ValueError("Stage 6 soak plan must include at least one channel.")

    channel_ids = [channel.channel_id for channel in plan.channels]
    if len(channel_ids) != len(set(channel_ids)):
        raise ValueError("Stage 6 soak plan channel IDs must be unique.")
    profiles = [channel.profile_id for channel in plan.channels]
    if len(profiles) != len(set(profiles)):
        raise ValueError("Stage 6 soak plan cannot duplicate a topology profile.")

    fault_count = 0
    for channel in plan.channels:
        if not channel.expected_outputs:
            raise ValueError(f"{channel.channel_id} must declare expected outputs.")
        if not channel.faults:
            raise ValueError(f"{channel.channel_id} must include at least one fault.")
        for fault in channel.faults:
            fault_count += 1
            if fault.recover_at_second <= fault.inject_at_second:
                raise ValueError(f"{fault.fault_id} recovery must occur after injection.")
            if fault.recover_at_second > plan.duration_seconds:
                raise ValueError(f"{fault.fault_id} recovery exceeds the soak duration.")

    missing_files = {
        "summary.json",
        "events.json",
        "profiles.json",
        "README.md",
        "support-bundle-manifest.json",
        "station-evidence-manifest.template.json",
    } - set(plan.support_bundle_files)
    if missing_files:
        raise ValueError(f"Stage 6 support bundle files missing: {sorted(missing_files)}")

    return {
        "duration_seconds": plan.duration_seconds,
        "channel_count": len(plan.channels),
        "fault_count": fault_count,
        "support_file_count": len(plan.support_bundle_files),
    }


def build_support_bundle_manifest(
    profiles: list[LabTopologyProfile], plan: SoakPlan
) -> dict[str, Any]:
    """Return a redacted support-bundle manifest for Stage 6-7 artifacts."""

    manifest = {
        "schema": "civiccast.lpm.support-bundle.v1",
        "profiles": [profile.profile_id for profile in profiles],
        "files": [
            {
                "path": path,
                "purpose": _support_file_purpose(path),
                "contains_secrets": False,
            }
            for path in plan.support_bundle_files
        ],
        "redaction": {
            "credential_values": "excluded",
            "device_passwords": "excluded",
            "operator_notes": "allowed after manual review",
        },
    }
    _assert_no_secret_fragments(manifest)
    manifest["manifest_sha256"] = _stable_sha256(manifest)
    return manifest


def build_field_evidence_template(
    profiles: list[LabTopologyProfile], support_bundle: dict[str, Any]
) -> dict[str, Any]:
    """Return the Stage 7 LPM evidence envelope template."""

    return {
        "schema": "civiccast.lpm.station-evidence.v1",
        "station": "Longmont Public Media",
        "station_evidence_status": "template-not-station-evidence",
        "support_bundle_manifest_sha256": support_bundle["manifest_sha256"],
        "profiles": [
            {
                "profile_id": profile.profile_id,
                "operator": "",
                "captured_at": "",
                "field_contact": "",
                "device_evidence": [
                    {
                        "device_id": "",
                        "proof_type": "connection-health|state-readback|cue-fire",
                        "observed_state": "",
                        "artifact_path": "",
                        "sha256": "",
                        "captured_at": "",
                        "notes": "",
                    }
                ],
                "media_evidence": [
                    {
                        "media_id": "",
                        "proof_type": "recording|stream|ffprobe|screenshot",
                        "duration_seconds": 0,
                        "artifact_path": "",
                        "sha256": "",
                        "captured_at": "",
                        "notes": "",
                    }
                ],
                "support_bundle": {"path": "", "sha256": ""},
                "notes": "Fill at LPM only after touching real station equipment.",
            }
            for profile in profiles
        ],
    }


def validate_field_evidence_bundle(
    bundle: dict[str, Any],
    *,
    evidence_root: Path | None = None,
    expected_profile_ids: Iterable[str] | None = None,
    expected_support_manifest_sha256: str | None = None,
) -> list[str]:
    """Return issues that prevent a bundle from earning station-device status."""

    issues: list[str] = []
    if bundle.get("schema") != "civiccast.lpm.station-evidence.v1":
        issues.append("station evidence schema must be civiccast.lpm.station-evidence.v1")
    if bundle.get("station") != "Longmont Public Media":
        issues.append("station must be Longmont Public Media")
    if bundle.get("station_evidence_status") != "station-captured":
        issues.append(
            "station_evidence_status must be station-captured for station-device evidence"
        )
    elif evidence_root is None:
        issues.append("evidence_root is required when station_evidence_status is station-captured")
    manifest_sha256 = str(bundle.get("support_bundle_manifest_sha256") or "")
    if not _is_sha256(manifest_sha256):
        issues.append("support_bundle_manifest_sha256 must be a lowercase SHA-256 hex digest")
    if (
        expected_support_manifest_sha256 is not None
        and manifest_sha256
        and manifest_sha256 != expected_support_manifest_sha256
    ):
        issues.append("support_bundle_manifest_sha256 does not match the support manifest")
    profiles = bundle.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        issues.append("profiles must be a non-empty list")
        return issues

    seen_profile_ids: list[str] = []
    for index, profile in enumerate(profiles):
        prefix = f"profiles[{index}]"
        if not isinstance(profile, dict):
            issues.append(f"{prefix} must be an object")
            continue
        profile_id = str(profile.get("profile_id") or "")
        if profile_id not in KNOWN_PROFILE_IDS:
            issues.append(f"{prefix}.profile_id is not a known LPM profile")
        elif profile_id in seen_profile_ids:
            issues.append(f"{prefix}.profile_id duplicates {profile_id}")
        else:
            seen_profile_ids.append(profile_id)
        for field_name in ("profile_id", "operator", "captured_at", "field_contact"):
            if not str(profile.get(field_name) or "").strip():
                issues.append(f"{prefix}.{field_name} is required")
        captured_at = str(profile.get("captured_at") or "")
        if captured_at and not _is_iso8601_timestamp(captured_at):
            issues.append(f"{prefix}.captured_at must be an ISO-8601 timestamp")
        issues.extend(
            _validate_evidence_items(
                profile.get("device_evidence"),
                prefix=f"{prefix}.device_evidence",
                required_fields=DEVICE_EVIDENCE_FIELDS,
                evidence_root=evidence_root,
            )
        )
        issues.extend(
            _validate_evidence_items(
                profile.get("media_evidence"),
                prefix=f"{prefix}.media_evidence",
                required_fields=MEDIA_EVIDENCE_FIELDS,
                evidence_root=evidence_root,
            )
        )
        support_bundle = profile.get("support_bundle")
        if not isinstance(support_bundle, dict):
            issues.append(f"{prefix}.support_bundle must be an object")
            continue
        if not str(support_bundle.get("path") or "").strip():
            issues.append(f"{prefix}.support_bundle.path is required")
        sha256 = str(support_bundle.get("sha256") or "")
        if not _is_sha256(sha256):
            issues.append(f"{prefix}.support_bundle.sha256 must be a lowercase SHA-256 hex digest")
        issues.extend(
            _validate_artifact_path_and_hash(
                support_bundle.get("path"),
                sha256,
                prefix=f"{prefix}.support_bundle",
                evidence_root=evidence_root,
            )
        )

    if expected_profile_ids is not None:
        expected = sorted(set(expected_profile_ids))
        observed = sorted(seen_profile_ids)
        if observed != expected:
            issues.append(
                "field evidence profiles must exactly match selected profiles: "
                f"expected {expected}, got {observed}"
            )

    try:
        _assert_no_secret_fragments(bundle)
    except ValueError as exc:
        issues.append(str(exc))
    return issues


def _validate_evidence_items(
    value: Any,
    *,
    prefix: str,
    required_fields: tuple[str, ...],
    evidence_root: Path | None,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(value, list) or not value:
        return [f"{prefix} must contain station evidence"]

    for index, item in enumerate(value):
        item_prefix = f"{prefix}[{index}]"
        if not isinstance(item, dict):
            issues.append(f"{item_prefix} must be an object")
            continue
        for field_name in required_fields:
            if field_name == "duration_seconds":
                duration = item.get(field_name)
                if not isinstance(duration, (int, float)) or duration <= 0:
                    issues.append(f"{item_prefix}.{field_name} must be > 0")
                continue
            field_value = str(item.get(field_name) or "").strip()
            if not field_value:
                issues.append(f"{item_prefix}.{field_name} is required")
            elif field_name == "captured_at" and not _is_iso8601_timestamp(field_value):
                issues.append(f"{item_prefix}.{field_name} must be an ISO-8601 timestamp")
        sha256 = str(item.get("sha256") or "")
        if sha256 and not _is_sha256(sha256):
            issues.append(f"{item_prefix}.sha256 must be a lowercase SHA-256 hex digest")
        issues.extend(
            _validate_artifact_path_and_hash(
                item.get("artifact_path"),
                sha256,
                prefix=item_prefix,
                evidence_root=evidence_root,
            )
        )
    return issues


def _validate_artifact_path_and_hash(
    artifact_path: Any,
    expected_sha256: str,
    *,
    prefix: str,
    evidence_root: Path | None,
) -> list[str]:
    issues: list[str] = []
    path_text = str(artifact_path or "").strip()
    if not path_text or evidence_root is None:
        return issues

    try:
        resolved = _resolve_relative_artifact(evidence_root, path_text)
    except ValueError as exc:
        return [f"{prefix}.path {exc}"]
    if not _is_sha256(expected_sha256):
        return issues
    if not resolved.is_file():
        return [f"{prefix}.path does not exist under evidence_root: {path_text}"]
    actual_sha256 = _file_sha256(resolved)
    if actual_sha256 != expected_sha256:
        issues.append(f"{prefix}.sha256 does not match artifact content")
    return issues


def summarize_stage67_events(events: list[LabEvent]) -> list[str]:
    """Return human-readable Stage 6-7 summary lines for artifact READMEs."""

    stage67 = [
        event
        for event in events
        if event.check_id.startswith("stage67-") or event.proof_source == "station-readiness"
    ]
    if not stage67:
        return ["Stage 6-7 soak/station-readiness: not run."]
    passed = sum(1 for event in stage67 if event.status == "passed")
    not_applicable = sum(1 for event in stage67 if event.status == "not-applicable")
    field_claims = [
        event
        for event in stage67
        if event.proof_level == "station-device-proven" or event.proof_source == "station-device"
    ]
    lines = [
        (
            f"Stage 6-7 soak/station-readiness: {passed} passed, {not_applicable} not applicable."
            if not_applicable
            else f"Stage 6-7 soak/station-readiness: {passed} passed."
        ),
        "Stage 6 soak is deterministic local rehearsal, not elapsed wall-clock soak proof.",
        "Stage 7 station-device evidence envelope is ready, but no station equipment is claimed locally.",
    ]
    if field_claims:
        lines.append("WARNING: station-device labels are present in this artifact.")
    else:
        lines.append("Station-device labels present: none.")
    return lines


def _channel_for_profile(profile: LabTopologyProfile) -> SoakChannel:
    if profile.profile_id == "fixed-studio-livestreaming":
        return SoakChannel(
            channel_id="channel-a-fixed-studio",
            profile_id=profile.profile_id,
            source_device_id="fixed-vmix-streaming-pc",
            expected_outputs=["local recording", "vMix stream output path"],
            faults=[
                SoakFault(
                    fault_id="fixed-decklink-signal-dropout",
                    device_class="decklink",
                    inject_at_second=900,
                    recover_at_second=960,
                    expected_effect="SDI signal unlock marks channel state stale.",
                    recovery_proof="Signal relock requires refreshed vMix/DeckLink state.",
                ),
                SoakFault(
                    fault_id="fixed-ndi-ptz-disappear-reappear",
                    device_class="ptz-visca-ndi",
                    inject_at_second=7200,
                    recover_at_second=7260,
                    expected_effect="NDI/PTZ state becomes unavailable without firing queued cues.",
                    recovery_proof="Reappeared NDI source refreshes PTZ availability.",
                ),
                SoakFault(
                    fault_id="fixed-tsr-sidecar-restart",
                    device_class="tsr-sidecar",
                    inject_at_second=14400,
                    recover_at_second=14430,
                    expected_effect="Device state is stale while the sidecar is unavailable.",
                    recovery_proof="Sidecar health and cue dispatcher state return before live fire.",
                ),
            ],
        )
    if profile.profile_id == "portable-field-kit":
        return SoakChannel(
            channel_id="channel-b-portable-field-kit",
            profile_id=profile.profile_id,
            source_device_id="portable-vmix-laptop",
            expected_outputs=["Castr", "LPM YouTube stream", "local recording"],
            faults=[
                SoakFault(
                    fault_id="portable-wifi-dropout-recovery",
                    device_class="network",
                    inject_at_second=1800,
                    recover_at_second=1860,
                    expected_effect="Egress state records dropout and suppresses fake success.",
                    recovery_proof="Retry/recovery marks stream target reachable after network returns.",
                ),
                SoakFault(
                    fault_id="portable-usb-capture-reset",
                    device_class="usb-capture",
                    inject_at_second=10800,
                    recover_at_second=10890,
                    expected_effect="USB capture identity becomes stale and dry runs invalidate.",
                    recovery_proof="Stable capture identity is re-read before later cue fire.",
                ),
                SoakFault(
                    fault_id="portable-atem-busy-transition",
                    device_class="atem",
                    inject_at_second=21600,
                    recover_at_second=21610,
                    expected_effect="Duplicate non-idempotent transition is refused.",
                    recovery_proof="Ordered cue dispatcher resumes after ATEM transition completes.",
                ),
            ],
        )
    if profile.profile_id == "digitization-obs":
        return SoakChannel(
            channel_id="channel-c-digitization-obs",
            profile_id=profile.profile_id,
            source_device_id="digitization-obs-studio",
            expected_outputs=["local recording"],
            faults=[
                SoakFault(
                    fault_id="digitization-obs-restart",
                    device_class="obs",
                    inject_at_second=2700,
                    recover_at_second=2760,
                    expected_effect="OBS websocket disconnect marks source/recording state stale.",
                    recovery_proof="GetVersion and event subscription succeed after reconnect.",
                ),
                SoakFault(
                    fault_id="digitization-deck-not-playing",
                    device_class="usb-capture",
                    inject_at_second=12600,
                    recover_at_second=12630,
                    expected_effect="Capture device exists but media content is not usable.",
                    recovery_proof="Recording readiness requires renewed source/media evidence.",
                ),
                SoakFault(
                    fault_id="digitization-obs-source-removed",
                    device_class="obs",
                    inject_at_second=30000,
                    recover_at_second=30060,
                    expected_effect="Removed OBS source invalidates cue-relevant dry run.",
                    recovery_proof="Source identity must be restored before recording proof.",
                ),
            ],
        )
    raise ValueError(f"No Stage 6 channel plan for profile: {profile.profile_id}")


def _soak_plan_claim(plan: SoakPlan) -> str:
    if len(plan.channels) == 3:
        return "Three-profile 12-hour soak/chaos plan covers local LPM lab parity."
    return "Selected-profile 12-hour soak/chaos plan covers local LPM lab evidence."


def _event_from_stage67_proof(proof: Stage67Proof) -> LabEvent:
    return LabEvent(
        profile_id=proof.profile_id,
        device_id=proof.device_id,
        check_id=proof.check_id,
        status=proof.status,
        proof_level=proof.proof_level,
        proof_source=proof.proof_source,
        claim=proof.claim,
        observed=proof.observed,
        not_claimed=proof.not_claimed,
        details=proof.details,
    )


def _support_file_purpose(path: str) -> str:
    if path.endswith("summary.json"):
        return "machine-readable run status"
    if path.endswith("events.json"):
        return "proof event rows"
    if path.endswith("profiles.json"):
        return "topology profile source"
    if path.endswith("README.md"):
        return "human-readable proof boundary"
    if "station-evidence" in path:
        return "LPM station-device evidence collection template"
    if "redacted" in path:
        return "redacted operational log"
    return "support evidence"


def _assert_no_secret_fragments(payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).lower()
    for fragment in FORBIDDEN_SECRET_FRAGMENTS:
        if fragment in body:
            raise ValueError(f"payload contains forbidden secret-looking text: {fragment}")


def _stable_sha256(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and value == value.lower()
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_iso8601_timestamp(value: str) -> bool:
    if "T" not in value:
        return False
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _resolve_relative_artifact(evidence_root: Path, path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        raise ValueError("must be relative to evidence_root")
    root = evidence_root.resolve(strict=False)
    resolved = (root / path).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("must stay inside evidence_root") from exc
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "DEVICE_EVIDENCE_FIELDS",
    "KNOWN_PROFILE_IDS",
    "MEDIA_EVIDENCE_FIELDS",
    "MIN_SOAK_SECONDS",
    "SoakChannel",
    "SoakFault",
    "SoakPlan",
    "Stage67Proof",
    "build_field_evidence_template",
    "build_stage67_proofs",
    "build_stage67_soak_plan",
    "build_support_bundle_manifest",
    "summarize_stage67_events",
    "validate_field_evidence_bundle",
    "validate_soak_plan",
]
