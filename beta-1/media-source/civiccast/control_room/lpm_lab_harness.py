# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Local LPM contract-lab harness.

Stage 1 turns the 3.2 LPM topology contract into checkable evidence. The harness
is intentionally local and deterministic: it proves CivicCast's profile/failure
contracts and records exactly which proof source was used. It does not claim
station-device evidence.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.control_room.lpm_lab import (
    DeviceContract,
    LabTopologyProfile,
    ProofLevel,
    TopologyId,
    build_lpm_lab_profiles,
    validate_lpm_lab_profiles,
)

EventStatus = Literal["passed", "failed", "not-applicable"]
ExecutionStage = Literal["catalog", "stage45", "stage67", "stage8"]
ProofSource = Literal[
    "profile-contract",
    "check-catalog",
    "stateful-simulator",
    "software-lab",
    "api-fixture",
    "station-readiness",
    "station-device",
    "release-hardening",
]

ARTIFACT_MARKER = ".civiccast-lpm-contract-lab-artifacts"
SOFTWARE_LAB_DEVICE_CLASSES = {"obs", "vmix"}


class CheckDefinition(BaseModel):
    """Static Stage 1 definition for one emitted contract-lab check."""

    model_config = ConfigDict(extra="forbid")

    status: EventStatus = "passed"
    proof_source: ProofSource
    proof_level: ProofLevel
    claim: Annotated[str, Field(min_length=1, max_length=300)]
    observed: Annotated[str, Field(min_length=1, max_length=600)] = (
        "Deterministic Stage 1 contract-lab simulator event completed."
    )
    not_claimed: Annotated[str | None, Field(max_length=600)] = (
        "This is not station-device evidence."
    )
    details: dict[str, Any] = Field(default_factory=dict)


def _check(
    _later_stage_source: ProofSource,
    _later_stage_level: ProofLevel,
    claim: str,
    *,
    status: EventStatus = "passed",
    observed: str = (
        "Required check is represented in the Stage 1 static check catalog; "
        "no live fixture, simulator, local software, or LPM device executed."
    ),
    not_claimed: str | None = (
        "This is not clean Windows install proof, real software proof, "
        "API fixture execution, simulator execution, or station-device evidence."
    ),
    details: dict[str, Any] | None = None,
) -> CheckDefinition:
    return CheckDefinition(
        status=status,
        proof_source="check-catalog",
        proof_level="mocked",
        claim=claim,
        observed=observed,
        not_claimed=not_claimed,
        details=details or {},
    )


CHECK_CATALOG: dict[str, CheckDefinition] = {
    "vmix-status-xml": _check(
        "api-fixture",
        "api-contract-proven",
        "vMix status XML parser accepts required fields and rejects drift.",
    ),
    "vmix-api-disabled": _check(
        "stateful-simulator",
        "simulated-proven",
        "Remote API disabled is surfaced as a controlled connection failure.",
    ),
    "vmix-auth-required-or-wrong": _check(
        "stateful-simulator",
        "simulated-proven",
        "vMix authentication requirement or wrong credentials fail closed.",
    ),
    "vmix-xml-required-field-drift": _check(
        "api-fixture",
        "api-contract-proven",
        "vMix status XML missing required fields refuses with schema drift evidence.",
    ),
    "vmix-input-identity-drift": _check(
        "stateful-simulator",
        "simulated-proven",
        "Input identity changes invalidate cue-relevant dry runs.",
    ),
    "vmix-source-removed-dry-run": _check(
        "stateful-simulator",
        "simulated-proven",
        "Source removal after dry run invalidates live fire for material cue state.",
    ),
    "vmix-usb-capture-input-select": _check(
        "api-fixture",
        "api-contract-proven",
        "Portable vMix USB capture input selection is modeled.",
    ),
    "vmix-usb-capture-identity-drift": _check(
        "stateful-simulator",
        "simulated-proven",
        "Portable vMix capture-device identity drift invalidates cue-relevant state.",
    ),
    "vmix-laptop-resource-ceiling": _check(
        "stateful-simulator",
        "simulated-proven",
        "Portable vMix laptop resource ceiling is represented as a warning/failure mode.",
    ),
    "vmix-record-state": _check(
        "api-fixture",
        "api-contract-proven",
        "vMix record state is represented in the portable workflow.",
    ),
    "vmix-stream-state": _check(
        "api-fixture",
        "api-contract-proven",
        "vMix stream state is represented in the portable workflow.",
    ),
    "tsr-sidecar-restart": _check(
        "stateful-simulator",
        "simulated-proven",
        "TSR sidecar restart is represented as stale state followed by recovery.",
    ),
    "decklink-driver-missing": _check(
        "stateful-simulator",
        "simulated-proven",
        "Missing Desktop Video driver is reported as no DeckLink API boundary.",
    ),
    "decklink-card-absent": _check(
        "stateful-simulator",
        "simulated-proven",
        "DeckLink card absence is represented without blocking non-DeckLink profiles.",
    ),
    "decklink-channel-absent": _check(
        "stateful-simulator",
        "simulated-proven",
        "Missing channel 2/3/4 is reported without blocking non-DeckLink profiles.",
    ),
    "decklink-mode-mismatch": _check(
        "stateful-simulator",
        "simulated-proven",
        "DeckLink mode mismatch is surfaced as a capture-readiness failure.",
    ),
    "decklink-signal-unlocked": _check(
        "stateful-simulator",
        "simulated-proven",
        "Signal-unlocked state remains station-device evidence pending.",
    ),
    "recording-decklink-preset-argv": _check(
        "api-fixture",
        "api-contract-proven",
        "A configured DeckLink SDI recording preset resolves to exact FFmpeg input arguments.",
    ),
    "ndi-source-present": _check(
        "stateful-simulator",
        "simulated-proven",
        "NDI source presence is represented in the PTZ state model.",
    ),
    "visca-udp-52381-ack": _check(
        "stateful-simulator",
        "simulated-proven",
        "VISCA UDP port 52381 ACK/completion sequence is modeled.",
    ),
    "ptz-camera-offline": _check(
        "stateful-simulator",
        "simulated-proven",
        "PTZ camera offline state fails closed and keeps control unavailable.",
    ),
    "visca-timeout": _check(
        "stateful-simulator",
        "simulated-proven",
        "VISCA timeout fails closed and keeps previous state.",
    ),
    "visca-command-not-executable": _check(
        "stateful-simulator",
        "simulated-proven",
        "VISCA command-not-executable response is represented distinctly from timeout.",
    ),
    "ndi-source-disappears": _check(
        "stateful-simulator",
        "simulated-proven",
        "NDI disappearance marks state stale instead of pretending control remains live.",
    ),
    "ndi-source-reappears": _check(
        "stateful-simulator",
        "simulated-proven",
        "NDI reappearance refreshes state before later live actions can trust it.",
    ),
    "ndi-source-rename": _check(
        "stateful-simulator",
        "simulated-proven",
        "An NDI source renamed in the discovery list is tracked by its stable id, not lost.",
    ),
    "ptz-credentials-rotated": _check(
        "stateful-simulator",
        "simulated-proven",
        "PTZ credential rotation is represented without storing public defaults.",
    ),
    "audio-topology-present": _check(
        "api-fixture",
        "api-contract-proven",
        "An audio-mixer topology declaration parses and rejects malformed/incomplete data.",
    ),
    "audio-control-not-claimed": _check(
        "api-fixture",
        "api-contract-proven",
        "The audio topology non-claim is enforced: a fixture cannot smuggle in a control surface.",
        status="not-applicable",
        observed="Stage 4-5 proves the non-claim is actively enforced, not just asserted.",
        not_claimed="No SQ5 command, state subscription, or station-device evidence is claimed.",
    ),
    "audio-sq-midi-mute": _check(
        "api-fixture",
        "api-contract-proven",
        "An Allen & Heath SQ-MIDI mute message parses per the published SQ MIDI Protocol.",
    ),
    "audio-midi-nrpn-message": _check(
        "api-fixture",
        "api-contract-proven",
        "A 4-frame MIDI NRPN Control Change write parses per the SQ MIDI Protocol and MIDI 1.0 spec.",
    ),
    "audio-midi-scene-recall": _check(
        "api-fixture",
        "api-contract-proven",
        "A MIDI Bank Select + Program Change scene-recall sequence parses per the SQ MIDI Protocol.",
    ),
    "atem-input-select": _check(
        "api-fixture",
        "api-contract-proven",
        "ATEM input select and transition state are modeled through the TSR contract.",
    ),
    "atem-absent": _check(
        "stateful-simulator",
        "simulated-proven",
        "ATEM unavailable is represented as an expected portable setup failure mode.",
    ),
    "atem-sdk-version-mismatch": _check(
        "api-fixture",
        "api-contract-proven",
        "ATEM SDK/protocol version mismatch refuses with explicit evidence.",
    ),
    "atem-busy-transition": _check(
        "stateful-simulator",
        "simulated-proven",
        "Busy transition refuses duplicate non-idempotent fire.",
    ),
    "atem-program-preview-state": _check(
        "api-fixture",
        "api-contract-proven",
        "ATEM program/preview state is represented for cue dry-run decisions.",
    ),
    "usb-capture-present": _check(
        "stateful-simulator",
        "simulated-proven",
        "USB capture identity is modeled as a generic UVC device.",
    ),
    "usb-capture-absent": _check(
        "stateful-simulator",
        "simulated-proven",
        "USB capture absence is an expected portable/digitization failure mode.",
    ),
    "usb-capture-wrong-uvc-name": _check(
        "stateful-simulator",
        "simulated-proven",
        "Unexpected UVC capture name is represented as identity drift.",
    ),
    "usb-capture-usb-reset": _check(
        "stateful-simulator",
        "simulated-proven",
        "USB capture reset produces stale input state before recovery.",
    ),
    "usb-capture-hdmi-signal-missing": _check(
        "stateful-simulator",
        "simulated-proven",
        "Missing HDMI signal is represented separately from missing USB device.",
    ),
    "usb-capture-identity-preserved": _check(
        "stateful-simulator",
        "simulated-proven",
        "USB capture identity is preserved across the vMix input mapping.",
    ),
    "recording-dshow-preset-argv": _check(
        "api-fixture",
        "api-contract-proven",
        "A configured DirectShow HDMI recording preset resolves to exact FFmpeg input arguments.",
    ),
    "usb-capture-deck-not-playing": _check(
        "stateful-simulator",
        "simulated-proven",
        "Digitization deck-not-playing state is represented as no usable capture content.",
    ),
    "elgato-obs-source-removed": _check(
        "stateful-simulator",
        "simulated-proven",
        "Elgato Video Capture OBS source removal invalidates recording readiness.",
    ),
    "local-recording-evidence": _check(
        "api-fixture",
        "api-contract-proven",
        "Local recording evidence is represented for the digitization OBS workflow.",
    ),
    "usb-audio-present": _check(
        "api-fixture",
        "api-contract-proven",
        "A usb-audio class device is discoverable in the capture-identity fixture.",
    ),
    "usb-audio-absent": _check(
        "api-fixture",
        "api-contract-proven",
        "A capture fixture with no usb-audio class device fails closed instead of assuming presence.",
    ),
    "usb-audio-sample-rate-mismatch": _check(
        "api-fixture",
        "api-contract-proven",
        "A USB/system audio sample rate that doesn't match the expected rate is rejected.",
    ),
    "usb-audio-sync-warning": _check(
        "api-fixture",
        "api-contract-proven",
        "A USB/system audio A/V delay at or over the sync-warning ceiling is rejected.",
    ),
    "wifi-latency-injection": _check(
        "stateful-simulator",
        "simulated-proven",
        "Portable WiFi latency injection is represented in evidence.",
    ),
    "wifi-dropout": _check(
        "stateful-simulator",
        "simulated-proven",
        "Portable WiFi dropout marks egress state stale and recoverable.",
    ),
    "dns-failure": _check(
        "stateful-simulator",
        "simulated-proven",
        "Portable DNS failure is represented as an egress dependency failure.",
    ),
    "castr-unreachable": _check(
        "stateful-simulator",
        "simulated-proven",
        "Castr unreachable is represented as an egress dependency failure.",
    ),
    "youtube-destination-confirmed": _check(
        "profile-contract",
        "api-contract-proven",
        "LPM YouTube stream destination is represented as a required portable egress target.",
    ),
    "egress-retry-recovery": _check(
        "stateful-simulator",
        "simulated-proven",
        "Portable egress retry/recovery is represented after transient network failure.",
    ),
    "obs-websocket-5-contract": _check(
        "api-fixture",
        "api-contract-proven",
        "OBS websocket 5.x request/response IDs and protocol version are modeled.",
    ),
    "obs-websocket-disabled": _check(
        "stateful-simulator",
        "simulated-proven",
        "OBS websocket disabled refuses with operator-readable setup guidance.",
    ),
    "obs-wrong-password": _check(
        "stateful-simulator",
        "simulated-proven",
        "OBS wrong-password failure is represented without logging the secret.",
    ),
    "obs-protocol-mismatch": _check(
        "stateful-simulator",
        "simulated-proven",
        "Protocol mismatch refuses with an operator-readable error.",
    ),
    "obs-source-missing": _check(
        "stateful-simulator",
        "simulated-proven",
        "Missing OBS source is represented as a cue-readiness failure.",
    ),
    "obs-source-removed": _check(
        "stateful-simulator",
        "simulated-proven",
        "Removed source invalidates cue-relevant dry runs.",
    ),
    "obs-recording-state": _check(
        "api-fixture",
        "api-contract-proven",
        "OBS recording state is represented in the digitization workflow.",
    ),
    "obs-restart": _check(
        "stateful-simulator",
        "simulated-proven",
        "OBS restart is represented as disconnect, stale state, and recovery.",
    ),
    "obs-event-subscription": _check(
        "api-fixture",
        "api-contract-proven",
        "OBS GeneralEvents subscription is represented as state-push evidence.",
    ),
}


class LabEvent(BaseModel):
    """One checkable contract-lab event."""

    model_config = ConfigDict(extra="forbid")

    profile_id: TopologyId
    device_id: Annotated[str, Field(min_length=1, max_length=120)]
    check_id: Annotated[str, Field(min_length=1, max_length=120)]
    status: EventStatus
    proof_level: ProofLevel
    proof_source: ProofSource
    claim: Annotated[str, Field(min_length=1, max_length=300)]
    observed: Annotated[str, Field(min_length=1, max_length=600)]
    not_claimed: Annotated[str | None, Field(max_length=600)] = None
    details: dict[str, Any] = Field(default_factory=dict)


class LabRunResult(BaseModel):
    """Machine-readable result for one local LPM contract-lab run."""

    model_config = ConfigDict(extra="forbid")

    run_id: Annotated[str, Field(min_length=1, max_length=80)]
    status: EventStatus
    execution_stage: ExecutionStage = "catalog"
    generated_at_unix: int
    profiles: list[TopologyId]
    events: list[LabEvent]
    issues: list[str] = Field(default_factory=list)


def _all_profiles() -> list[LabTopologyProfile]:
    profiles = build_lpm_lab_profiles()
    return [
        profiles["fixed-studio-livestreaming"],
        profiles["portable-field-kit"],
        profiles["digitization-obs"],
    ]


def _select_profiles(profile_ids: Iterable[str] | None) -> list[LabTopologyProfile]:
    profiles = build_lpm_lab_profiles()
    if profile_ids is None:
        return _all_profiles()

    profile_id_list = list(profile_ids)
    if not profile_id_list:
        raise ValueError("At least one LPM Lab profile is required.")

    selected: list[LabTopologyProfile] = []
    unknown: list[str] = []
    if "all" in profile_id_list:
        if len(profile_id_list) > 1:
            raise ValueError("Use --profile all by itself; do not combine it with other profiles.")
        return _all_profiles()

    for profile_id in profile_id_list:
        if profile_id in profiles:
            selected.append(profiles[profile_id])
        else:
            unknown.append(profile_id)
    if unknown:
        known = ", ".join(sorted(profiles))
        raise ValueError(f"Unknown LPM Lab profile(s): {', '.join(unknown)}. Known: {known}.")
    if not selected:
        raise ValueError("At least one LPM Lab profile is required.")
    return selected


def simulate_profile(profile: LabTopologyProfile) -> list[LabEvent]:
    """Return deterministic simulator/API-contract events for a profile."""

    events: list[LabEvent] = [
        LabEvent(
            profile_id=profile.profile_id,
            device_id="profile",
            check_id="profile-contract-loaded",
            status="passed",
            proof_level="mocked",
            proof_source="profile-contract",
            claim=f"{profile.label} profile is loaded with {len(profile.devices)} device contracts.",
            observed="Profile schema validated and all declared required absences/claims are present.",
            not_claimed="This profile load is not API fixture execution or station-device evidence.",
            details={
                "required_absences": profile.required_absences,
                "egress_destinations": profile.egress_destinations,
                "source_ids": [source.source_id for source in profile.sources],
            },
        )
    ]

    for device in profile.devices:
        for check_id in device.required_checks:
            events.append(_event_from_catalog(profile.profile_id, device, check_id))
    return events


def _event_from_catalog(profile_id: TopologyId, device: DeviceContract, check_id: str) -> LabEvent:
    definition = CHECK_CATALOG.get(check_id)
    if definition is None:
        return LabEvent(
            profile_id=profile_id,
            device_id=device.contract_id,
            check_id=check_id,
            status="failed",
            proof_level="mocked",
            proof_source="profile-contract",
            claim=f"Required check {check_id!r} has a Stage 1 definition.",
            observed="No Stage 1 check definition exists.",
            not_claimed="No proof is claimed for an undefined check.",
            details={"device_class": device.device_class, "device_label": device.label},
        )

    details = {
        "device_class": device.device_class,
        "device_label": device.label,
        "device_contract_proof_level": device.proof_level,
        **definition.details,
    }
    return LabEvent(
        profile_id=profile_id,
        device_id=device.contract_id,
        check_id=check_id,
        status=definition.status,
        proof_level=definition.proof_level,
        proof_source=definition.proof_source,
        claim=definition.claim,
        observed=definition.observed,
        not_claimed=definition.not_claimed,
        details=details,
    )


def _coverage_issues(profiles: list[LabTopologyProfile], events: list[LabEvent]) -> list[str]:
    if not profiles:
        return ["At least one LPM Lab profile must be selected."]

    issues: list[str] = []
    emitted = {(event.device_id, event.check_id) for event in events}
    for profile in profiles:
        for device in profile.devices:
            for check_id in device.required_checks:
                if check_id not in CHECK_CATALOG:
                    issues.append(
                        f"{profile.profile_id}/{device.contract_id} requires undefined check {check_id}."
                    )
                if (device.contract_id, check_id) not in emitted:
                    issues.append(
                        f"{profile.profile_id}/{device.contract_id} did not emit required check {check_id}."
                    )
    return issues


def run_lpm_contract_lab(
    *,
    profile_ids: Iterable[str] | None = None,
    run_id: str | None = None,
    artifact_root: Path | None = None,
    force_clean: bool = False,
    execution_stage: ExecutionStage = "catalog",
    probe_real_software: bool = False,
    require_software_lab: bool = False,
) -> LabRunResult:
    """Run the local deterministic LPM contract lab and optionally write artifacts."""

    selected = _select_profiles(profile_ids)
    all_profiles = build_lpm_lab_profiles()
    events: list[LabEvent] = []
    for profile in selected:
        events.extend(simulate_profile(profile))
        if execution_stage in {"stage45", "stage67", "stage8"}:
            from civiccast.control_room.lpm_lab_stage45 import (
                build_stage45_proofs_for_profile,
            )

            events.extend(
                build_stage45_proofs_for_profile(
                    profile,
                    probe_real_software=probe_real_software,
                )
            )
        elif execution_stage != "catalog":
            raise ValueError(f"Unknown LPM Lab execution stage: {execution_stage}")
    if execution_stage in {"stage67", "stage8"}:
        from civiccast.control_room.lpm_lab_stage67 import build_stage67_proofs

        events.extend(build_stage67_proofs(selected))
    if execution_stage == "stage8":
        from civiccast.control_room.lpm_lab_stage8 import build_stage8_proofs

        events.extend(build_stage8_proofs(selected))
    issues = validate_lpm_lab_profiles(all_profiles)
    issues.extend(_coverage_issues(selected, events))
    if require_software_lab:
        missing = _missing_required_software_lab_classes(selected, events)
        if missing:
            issues.append(
                "Stage 4 software-lab proof was required for selected profile "
                f"software class(es) {', '.join(missing)}, but no passed local "
                "software probe was recorded for each required class."
            )
    status: EventStatus = (
        "failed" if issues or any(event.status == "failed" for event in events) else "passed"
    )
    result = LabRunResult(
        run_id=run_id or f"lpm-contract-lab-{int(time.time())}",
        status=status,
        execution_stage=execution_stage,
        generated_at_unix=int(time.time()),
        profiles=[profile.profile_id for profile in selected],
        events=events,
        issues=issues,
    )
    if artifact_root is not None:
        write_lpm_contract_lab_artifacts(result, artifact_root, force_clean=force_clean)
    return result


def _missing_required_software_lab_classes(
    profiles: list[LabTopologyProfile], events: list[LabEvent]
) -> list[str]:
    required = sorted(
        {
            device.device_class
            for profile in profiles
            for device in profile.devices
            if device.device_class in SOFTWARE_LAB_DEVICE_CLASSES
        }
    )
    passed = {
        str(event.details.get("device_class"))
        for event in events
        if event.status == "passed"
        and event.proof_source == "software-lab"
        and event.proof_level == "software-lab-proven"
    }
    return [device_class for device_class in required if device_class not in passed]


def summarize_software_lab(result: LabRunResult) -> list[str]:
    """Return human-readable Stage 4 software probe status lines."""

    lines: list[str] = []
    for software_class in sorted(SOFTWARE_LAB_DEVICE_CLASSES):
        events = [
            event
            for event in result.events
            if event.proof_source == "software-lab"
            and event.details.get("device_class") == software_class
        ]
        label = "OBS" if software_class == "obs" else "vMix"
        if not events:
            lines.append(f"{label} software lab: not run.")
            continue
        passed = [
            event
            for event in events
            if event.status == "passed" and event.proof_level == "software-lab-proven"
        ]
        if passed:
            lines.append(f"{label} software lab: passed; {_md(passed[0].observed)}")
            continue
        failed = [event for event in events if event.status == "failed"]
        if failed:
            lines.append(f"{label} software lab: failed; {_md(failed[0].observed)}")
            continue
        lines.append(
            f"{label} software lab: not reached; no software-lab proof claimed. "
            f"{_md(events[0].observed)}"
        )
    lines.append("Station-device evidence: none in this local artifact.")
    return lines


def write_lpm_contract_lab_artifacts(
    result: LabRunResult, artifact_root: Path, *, force_clean: bool = False
) -> None:
    cleaned = _prepare_artifact_root(artifact_root, force_clean=force_clean)
    (artifact_root / "summary.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (artifact_root / "events.json").write_text(
        json.dumps([event.model_dump(mode="json") for event in result.events], indent=2),
        encoding="utf-8",
    )
    (artifact_root / "profiles.json").write_text(
        json.dumps(
            {
                key: profile.model_dump(mode="json")
                for key, profile in build_lpm_lab_profiles().items()
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if cleaned:
        (artifact_root / "artifact-root-cleanup.json").write_text(
            json.dumps({"force_clean": True, "stale_artifacts_removed": True}, indent=2),
            encoding="utf-8",
        )
    (artifact_root / ARTIFACT_MARKER).write_text(
        "CivicCast LPM contract-lab artifact root. Safe for contract-lab cleanup only.\n",
        encoding="utf-8",
    )
    if result.execution_stage in {"stage67", "stage8"}:
        _write_stage67_artifacts(result, artifact_root)
    if result.execution_stage == "stage8":
        _write_stage8_artifacts(result, artifact_root)
    (artifact_root / "README.md").write_text(_render_markdown_summary(result), encoding="utf-8")


def _write_stage67_artifacts(result: LabRunResult, artifact_root: Path) -> None:
    stage67_by_check = {event.check_id: event for event in result.events}
    soak_plan = stage67_by_check.get("stage67-three-channel-soak-plan")
    support_bundle = stage67_by_check.get("stage67-support-bundle-redaction")
    field_envelope = stage67_by_check.get("stage67-station-evidence-envelope")

    if soak_plan is not None:
        (artifact_root / "stage67-soak-plan.json").write_text(
            json.dumps(soak_plan.details, indent=2),
            encoding="utf-8",
        )
    if support_bundle is not None:
        (artifact_root / "support-bundle-manifest.json").write_text(
            json.dumps(support_bundle.details, indent=2),
            encoding="utf-8",
        )
    if field_envelope is not None:
        (artifact_root / "station-evidence-manifest.template.json").write_text(
            json.dumps(field_envelope.details.get("template", {}), indent=2),
            encoding="utf-8",
        )

    adapter_log_dir = artifact_root / "adapter-logs"
    adapter_log_dir.mkdir(exist_ok=True)
    (adapter_log_dir / "redacted-device-control.log").write_text(
        "Stage 6-7 local rehearsal log. No credentials or station secrets captured.\n",
        encoding="utf-8",
    )
    proof_log_dir = artifact_root / "proof-log"
    proof_log_dir.mkdir(exist_ok=True)
    (proof_log_dir / "redacted-control-room-actions.jsonl").write_text(
        json.dumps(
            {
                "event": "stage67-local-rehearsal",
                "station_device_evidence": False,
                "redacted": True,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _write_stage8_artifacts(result: LabRunResult, artifact_root: Path) -> None:
    from civiccast.control_room.lpm_lab_stage8 import write_stage8_artifacts

    profiles = build_lpm_lab_profiles()
    selected = [profiles[profile_id] for profile_id in result.profiles]
    write_stage8_artifacts(artifact_root, selected, events=result.events)


def _prepare_artifact_root(artifact_root: Path, *, force_clean: bool) -> bool:
    if artifact_root.exists() and not artifact_root.is_dir():
        raise NotADirectoryError(f"Artifact root exists and is not a directory: {artifact_root}")

    cleaned = False
    if artifact_root.exists():
        children = list(artifact_root.iterdir())
        if children and not force_clean:
            raise FileExistsError(
                f"Artifact root already contains files: {artifact_root}. "
                "Use --force-clean / force_clean=True only for a marked contract-lab "
                "artifact root under repo artifacts/ or system temp."
            )
        if children and force_clean:
            _assert_safe_force_clean_root(artifact_root)
            for child in children:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            cleaned = True
    artifact_root.mkdir(parents=True, exist_ok=True)
    return cleaned


def _assert_safe_force_clean_root(artifact_root: Path) -> None:
    resolved = artifact_root.resolve(strict=False)
    repo_artifacts = Path(__file__).resolve().parents[2] / "artifacts"
    safe_roots = [repo_artifacts.resolve(strict=False), Path(tempfile.gettempdir()).resolve()]

    if any(resolved == safe_root for safe_root in safe_roots) or not any(
        _is_relative_to(resolved, safe_root) for safe_root in safe_roots
    ):
        raise ValueError(
            "Refusing force_clean outside a safe child artifact root. "
            "Choose a dedicated directory under the repo artifacts folder or system temp."
        )

    marker = artifact_root / ARTIFACT_MARKER
    if not marker.is_file():
        raise ValueError(
            "Refusing force_clean because the artifact root is not marked as a "
            "CivicCast LPM contract-lab artifact directory."
        )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _render_markdown_summary(result: LabRunResult) -> str:
    profiles = build_lpm_lab_profiles()
    lines = [
        "# LPM Contract Lab Run",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Status: `{result.status}`",
        f"- Execution stage: `{result.execution_stage}`",
        f"- Profiles: {', '.join(result.profiles)}",
        f"- Events: {len(result.events)}",
        f"- Issues: {len(result.issues)}",
        "",
        "This is local contract-lab evidence. It does not include station-device evidence.",
        "",
        "## Proof Boundary",
        "",
        _proof_boundary_line(result.execution_stage),
        (
            "It is not clean Windows install proof, not real OBS/vMix/NDI software proof,"
            if result.execution_stage == "catalog"
            else "It is not clean Windows install proof; local software proof exists only for rows whose software probe passed,"
        ),
        "not station-device evidence, and not a beta/release publication decision.",
        "",
    ]
    if result.execution_stage in {"stage45", "stage67", "stage8"}:
        lines.extend(
            [
                "## Read First - Software Probe Summary",
                "",
                *[f"- {line}" for line in summarize_software_lab(result)],
                "",
            ]
        )
    if result.execution_stage in {"stage67", "stage8"}:
        from civiccast.control_room.lpm_lab_stage67 import summarize_stage67_events

        lines.extend(
            [
                "## Read First - Stage 6-7 Soak And Station Readiness",
                "",
                *[f"- {line}" for line in summarize_stage67_events(result.events)],
                "",
            ]
        )
    if result.execution_stage == "stage8":
        from civiccast.control_room.lpm_lab_stage8 import summarize_stage8_events

        lines.extend(
            [
                "## Read First - Stage 8 Local Release Hardening",
                "",
                *[f"- {line}" for line in summarize_stage8_events(result.events)],
                "",
            ]
        )
    lines.extend(
        [
            "## Proof Label Legend",
            "",
            "- `mocked`: rehearsal/context only; not release evidence.",
            "- `simulated-proven`: passed the stateful simulator or local fault harness.",
            "- `software-lab-proven`: passed against real local software.",
            "- `api-contract-proven`: implemented and exercised against API fixtures/contracts.",
            "- station-device evidence labels: reserved for station equipment evidence.",
            "",
            "Station-readiness rows are collection/template checks only. They are useful",
            "for preparing a station run but do not mean station equipment was touched.",
            "",
            "Stage 0-1 event rows use evidence source `check-catalog` and proof label",
            "`mocked` unless a later stage has actually executed the referenced fixture,",
            "simulator, local software, or station device.",
            "",
            "## Profile Boundaries",
            "",
        ]
    )
    for profile_id in result.profiles:
        profile = profiles[profile_id]
        lines.extend(
            [
                f"### {profile.label}",
                "",
                "**Claims:**",
                "",
                *[f"- {claim}" for claim in profile.claims],
                "",
                "**Not claimed:**",
                "",
                *[f"- {not_claimed}" for not_claimed in profile.not_claimed],
                "",
            ]
        )

    lines.extend(
        [
            "## Events",
            "",
            "| Profile | Device | Check | Status | Proof label | Evidence source | Claim | Observed | Not claimed |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for event in result.events:
        lines.append(
            f"| `{event.profile_id}` | `{event.device_id}` | `{event.check_id}` | "
            f"`{event.status}` | `{event.proof_level}` | `{event.proof_source}` | "
            f"{_md(event.claim)} | {_md(event.observed)} | {_md(event.not_claimed or '')} |"
        )
    if result.issues:
        lines.extend(["", "## Issues", ""])
        lines.extend(f"- {issue}" for issue in result.issues)
    return "\n".join(lines) + "\n"


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _proof_boundary_line(execution_stage: ExecutionStage) -> str:
    if execution_stage == "catalog":
        return "This artifact proves only the local Stage 0-1 deterministic contract lab."
    if execution_stage == "stage8":
        return (
            "This artifact includes Stage 4-5 fixtures/probes, Stage 6-7 deterministic "
            "rehearsal, and Stage 8 local release-hardening files."
        )
    return "This artifact includes opt-in Stage 4-5 local API fixtures, simulators, and software probes."


__all__ = [
    "CHECK_CATALOG",
    "CheckDefinition",
    "EventStatus",
    "ExecutionStage",
    "LabEvent",
    "LabRunResult",
    "ProofSource",
    "run_lpm_contract_lab",
    "simulate_profile",
    "summarize_software_lab",
    "write_lpm_contract_lab_artifacts",
]
