# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""LPM 3.2 livestreaming contract-lab profiles.

The profiles in this module are not device drivers. They are the executable
contract that Stage 0 of the 3.2 lab uses to keep implementation, tests, docs,
and release language honest about which LPM topology is being proven.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ProofLevel = Literal[
    "mocked",
    "simulated-proven",
    "software-lab-proven",
    "api-contract-proven",
    "station-device-proven",
]

SourceType = Literal[
    "direct-lpm-doc",
    "vendor-doc",
    "civiccast-inference",
    "station-device-confirmed",
]

TopologyId = Literal[
    "fixed-studio-livestreaming",
    "portable-field-kit",
    "digitization-obs",
]


class SourceRecord(BaseModel):
    """Structured source basis for one or more profile claims."""

    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=180)]
    url: Annotated[str, Field(min_length=1, max_length=300)]
    accessed_at: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    source_type: SourceType
    claim_ids: list[Annotated[str, Field(min_length=1, max_length=120)]]


class DeviceContract(BaseModel):
    """One device/API surface CivicCast must model for a lab topology."""

    model_config = ConfigDict(extra="forbid")

    contract_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    device_class: Annotated[str, Field(min_length=1, max_length=80)]
    integration_surface: Annotated[str, Field(min_length=1, max_length=160)]
    proof_level: ProofLevel
    required_for_profile: bool = True
    capabilities: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list
    )
    failure_modes: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(
        default_factory=list
    )
    required_checks: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list
    )
    evidence_basis: Annotated[str, Field(min_length=1, max_length=500)]
    station_device_evidence_required: bool = True
    credential_storage_policy: Annotated[str, Field(min_length=1, max_length=300)] = (
        "No plaintext secrets in profiles, fixtures, logs, support bundles, or docs; "
        "runtime credentials are operator-provided through the credential store."
    )


class LabTopologyProfile(BaseModel):
    """A complete LPM production topology CivicCast must prove against."""

    model_config = ConfigDict(extra="forbid")

    profile_id: TopologyId
    label: Annotated[str, Field(min_length=1, max_length=160)]
    priority: Annotated[int, Field(ge=1, le=10)]
    purpose: Annotated[str, Field(min_length=1, max_length=600)]
    sources: list[SourceRecord]
    devices: list[DeviceContract]
    network_profile: Annotated[str, Field(min_length=1, max_length=300)]
    required_absences: list[Annotated[str, Field(min_length=1, max_length=180)]] = Field(
        default_factory=list
    )
    egress_destinations: list[Annotated[str, Field(min_length=1, max_length=160)]] = Field(
        default_factory=list
    )
    claims: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(default_factory=list)
    not_claimed: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        default_factory=list
    )

    @property
    def device_ids(self) -> set[str]:
        return {device.contract_id for device in self.devices}


def _source(
    source_id: str,
    title: str,
    url: str,
    source_type: SourceType,
    claim_ids: list[str],
) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        title=title,
        url=url,
        accessed_at="2026-06-30",
        source_type=source_type,
        claim_ids=claim_ids,
    )


def build_lpm_lab_profiles() -> dict[TopologyId, LabTopologyProfile]:
    """Return the canonical 3.2 LPM Lab topology profiles."""

    fixed = LabTopologyProfile(
        profile_id="fixed-studio-livestreaming",
        label="Fixed Studio + Livestreaming Studio",
        priority=1,
        purpose=(
            "Primary station path: TV Studio cameras/audio route to the Livestreaming "
            "Studio, where vMix on the streaming PC sees SDI/DeckLink and NDI/PTZ "
            "sources."
        ),
        sources=[
            _source(
                "lpm-media-studio-public",
                "LPM Media Studio public page",
                "https://longmontpublicmedia.org/space/media-studio/",
                "direct-lpm-doc",
                ["fixed-cameras", "fixed-audio", "fixed-aida-ptz", "fixed-sdi-route"],
            ),
            _source(
                "lpm-tv-studio-wiki",
                "LPM TV Studio wiki",
                "https://wiki.longmontpublicmedia.org/makerspace/studios/tv-studio",
                "direct-lpm-doc",
                ["fixed-decklink-vmix-inputs"],
            ),
            _source(
                "lpm-soundpost-wiki",
                "LPM Soundpost Sessions video setup",
                "https://wiki.longmontpublicmedia.org/production-how-tos/soundpost-sessions-concert-set-up-video-only",
                "direct-lpm-doc",
                ["fixed-decklink-vmix-inputs"],
            ),
            _source(
                "lpm-live-streaming-gaming-studio-wiki",
                "LPM Live Streaming / Gaming Studio wiki",
                "https://wiki.longmontpublicmedia.org/makerspace/studios/live-streaming-gaming-studio",
                "direct-lpm-doc",
                ["fixed-vmix", "fixed-sdi-inputs", "fixed-ndi", "fixed-ptz-controller"],
            ),
            _source(
                "lpm-aida-ptz-wiki",
                "LPM AIDA NDI PTZ cameras wiki",
                "https://wiki.longmontpublicmedia.org/production-how-tos/aida-ndi-ptz-cameras",
                "direct-lpm-doc",
                ["fixed-aida-visca-udp", "fixed-aida-serial-fallback"],
            ),
            _source(
                "blackmagic-decklink-product",
                "Blackmagic DeckLink product family",
                "https://www.blackmagicdesign.com/products/decklink",
                "vendor-doc",
                ["fixed-decklink-duo2-product-basis"],
            ),
            _source(
                "civiccast-decklink-duo-channel-inference",
                "CivicCast inference: LPM DeckLink Duo 2/3/4 wording",
                "local:civiccast/docs/spec/3.2-lpm-livestreaming-contract-lab.md",
                "civiccast-inference",
                ["fixed-decklink-duo2-channels-2-3-4"],
            ),
        ],
        network_profile="Wired/private studio network with SDI and NDI present.",
        devices=[
            DeviceContract(
                contract_id="fixed-vmix-streaming-pc",
                label="vMix Streaming PC",
                device_class="vmix",
                integration_surface="vMix HTTP /api plus status XML and TCP/TALLY where useful",
                proof_level="mocked",
                capabilities=[
                    "input select",
                    "preview/program state",
                    "cut/fade",
                    "overlay in/out",
                    "scoped title/text update",
                    "status and tally readback",
                ],
                failure_modes=[
                    "remote API disabled",
                    "auth required or wrong",
                    "XML required-field drift",
                    "input identity change",
                    "source removed while dry-run preview is open",
                ],
                required_checks=[
                    "vmix-status-xml",
                    "vmix-api-disabled",
                    "vmix-auth-required-or-wrong",
                    "vmix-xml-required-field-drift",
                    "vmix-input-identity-drift",
                    "vmix-source-removed-dry-run",
                    "tsr-sidecar-restart",
                ],
                evidence_basis="vMix is the documented streaming PC software for the LPM path.",
            ),
            DeviceContract(
                contract_id="fixed-decklink-duo-2-channels-2-3-4",
                label="DeckLink Duo camera channels 2, 3, and 4",
                device_class="decklink",
                integration_surface="Blackmagic Desktop Video / DeckLink SDK / vMix camera inputs",
                proof_level="mocked",
                capabilities=[
                    "enumerate card/channel presence",
                    "detect no-card and no-signal states",
                    "preserve channel identity through vMix input mapping",
                ],
                failure_modes=[
                    "DeckLink driver missing",
                    "card absent",
                    "channel absent",
                    "mode mismatch",
                    "signal unlocked",
                ],
                required_checks=[
                    "decklink-driver-missing",
                    "decklink-card-absent",
                    "decklink-channel-absent",
                    "decklink-mode-mismatch",
                    "decklink-signal-unlocked",
                    "recording-decklink-preset-argv",
                ],
                evidence_basis=(
                    "LPM docs name DeckLink Duo 2, 3, and 4 as vMix inputs; Blackmagic "
                    "lists DeckLink Duo 2 as a four-channel product, so one Duo 2 card "
                    "using channels 2/3/4 is the best-read inference, not a direct LPM claim."
                ),
            ),
            DeviceContract(
                contract_id="fixed-aida-ndi-ptz",
                label="AIDA NDI PTZ cameras",
                device_class="ptz-visca-ndi",
                integration_surface="NDI discovery/readback plus Sony VISCA over UDP port 52381",
                proof_level="mocked",
                capabilities=["preset recall", "position state", "NDI presence", "VISCA ACK/error"],
                failure_modes=[
                    "camera offline",
                    "VISCA timeout",
                    "command not executable",
                    "NDI source disappears",
                    "NDI source renamed",
                    "credentials rotated from public defaults",
                ],
                required_checks=[
                    "ndi-source-present",
                    "visca-udp-52381-ack",
                    "ptz-camera-offline",
                    "visca-timeout",
                    "visca-command-not-executable",
                    "ndi-source-disappears",
                    "ndi-source-reappears",
                    "ndi-source-rename",
                    "ptz-credentials-rotated",
                ],
                evidence_basis=(
                    "LPM AIDA wiki identifies three NDI PTZ cameras, fixed private IPs, "
                    "Sony VISCA UDP port 52381, and serial VISCA 9600 baud fallback."
                ),
            ),
            DeviceContract(
                contract_id="fixed-allen-heath-sq5",
                label="Allen & Heath SQ5",
                device_class="audio-mixer",
                integration_surface=(
                    "audio topology proof plus SQ MIDI Protocol Issue 5 message-format "
                    "validation (MIDI-over-TCP port 51325); no live console connection"
                ),
                proof_level="mocked",
                capabilities=[
                    "documented audio-path presence",
                    "support-bundle topology entry",
                    "SQ-MIDI mute message format",
                    "SQ-MIDI NRPN fader/parameter message format",
                    "SQ-MIDI scene recall message format",
                ],
                failure_modes=[
                    "audio not present",
                    "audio path lacks station-device evidence",
                    "malformed SQ-MIDI message rejected",
                ],
                required_checks=[
                    "audio-topology-present",
                    "audio-control-not-claimed",
                    "audio-sq-midi-mute",
                    "audio-midi-nrpn-message",
                    "audio-midi-scene-recall",
                ],
                evidence_basis=(
                    "LPM public Media Studio page names Allen & Heath SQ5. Allen & Heath's own "
                    "SQ MIDI Protocol Issue 5 PDF documents MIDI-over-TCP port 51325, NRPN "
                    "fader/mute writes, and bank-change+program-change scene recall."
                ),
                station_device_evidence_required=False,
            ),
        ],
        egress_destinations=["local recording", "stream output path configured in vMix"],
        claims=[
            "CivicCast can model the primary fixed-studio livestreaming path as a Stage 0-1 contract.",
            "Device-control rows remain mocked/check-catalog evidence until later stages execute simulators, API fixtures, software, or station devices.",
        ],
        not_claimed=[
            "No claim that SDI signal lock has station-device evidence before LPM hardware evidence.",
            "No claim that AIDA credentials from public wiki are valid or should be reused.",
        ],
    )

    portable = LabTopologyProfile(
        profile_id="portable-field-kit",
        label="Portable Field Kit",
        priority=2,
        purpose=(
            "Mobile livestreaming path: Panasonic cameras feed an ATEM Mini Extreme, "
            "the ATEM output reaches a vMix laptop through a Cam Link-type USB capture "
            "device, and the stream goes to Castr and YouTube."
        ),
        sources=[
            _source(
                "lpm-outdoor-concert-wiki",
                "LPM Outdoor Summer Concert setup",
                "https://wiki.longmontpublicmedia.org/production-how-tos/outdoor-summer-concert-series-setup-in-field-set-up",
                "direct-lpm-doc",
                [
                    "portable-panasonic-cameras",
                    "portable-atem-mini-extreme",
                    "portable-camlink-type-capture",
                    "portable-uphoria-audio",
                    "portable-vmix-laptop",
                    "portable-wifi-hotspot",
                    "portable-castr-youtube",
                ],
            ),
        ],
        network_profile="Portable laptop workflow with optional WiFi/hotspot and latency/dropout risk.",
        required_absences=["DeckLink card", "AIDA/PTZ target", "wired studio LAN assumption"],
        devices=[
            DeviceContract(
                contract_id="portable-vmix-laptop",
                label="vMix Laptop",
                device_class="vmix",
                integration_surface="vMix HTTP /api plus status XML on portable machine",
                proof_level="mocked",
                capabilities=[
                    "USB capture input select",
                    "record",
                    "stream state",
                    "audio delay awareness",
                ],
                failure_modes=[
                    "laptop resource ceiling",
                    "API disabled",
                    "USB capture identity drift",
                ],
                required_checks=[
                    "vmix-status-xml",
                    "vmix-api-disabled",
                    "vmix-usb-capture-input-select",
                    "vmix-usb-capture-identity-drift",
                    "vmix-laptop-resource-ceiling",
                    "vmix-record-state",
                    "vmix-stream-state",
                ],
                evidence_basis="LPM Outdoor Concert setup names vMix laptop.",
            ),
            DeviceContract(
                contract_id="portable-atem-mini-extreme",
                label="ATEM Mini Extreme",
                device_class="atem",
                integration_surface="Blackmagic ATEM SDK / TSR ATEM device contract",
                proof_level="mocked",
                capabilities=["program/preview model", "input select", "transition state"],
                failure_modes=["switcher absent", "SDK version mismatch", "busy transition"],
                required_checks=[
                    "atem-input-select",
                    "atem-absent",
                    "atem-sdk-version-mismatch",
                    "atem-busy-transition",
                    "atem-program-preview-state",
                ],
                evidence_basis="LPM Outdoor Concert setup names ATEM Mini Extreme.",
            ),
            DeviceContract(
                contract_id="portable-usb-capture-camlink-type",
                label="Cam Link-type USB capture",
                device_class="usb-capture",
                integration_surface="Windows capture-device enumeration and vMix input identity",
                proof_level="mocked",
                capabilities=[
                    "detect UVC capture device",
                    "detect no-device path",
                    "preserve input identity",
                ],
                failure_modes=[
                    "device not present",
                    "wrong UVC name",
                    "USB reset",
                    "HDMI signal missing",
                ],
                required_checks=[
                    "usb-capture-present",
                    "usb-capture-absent",
                    "usb-capture-wrong-uvc-name",
                    "usb-capture-usb-reset",
                    "usb-capture-hdmi-signal-missing",
                    "usb-capture-identity-preserved",
                    "recording-dshow-preset-argv",
                ],
                evidence_basis=(
                    "LPM Outdoor Concert setup says cam link/camlink; brand is not confirmed "
                    "in LPM's own docs, so CivicCast treats it as generic USB capture."
                ),
            ),
            DeviceContract(
                contract_id="portable-behringer-u-phoria",
                label="Behringer U-Phoria USB audio",
                device_class="usb-audio",
                integration_surface="Windows audio-device enumeration and vMix audio input identity",
                proof_level="mocked",
                capabilities=["audio-device presence", "missing-audio path", "sync/delay note"],
                failure_modes=["device absent", "wrong sample rate", "audio out of sync"],
                required_checks=[
                    "usb-audio-present",
                    "usb-audio-absent",
                    "usb-audio-sample-rate-mismatch",
                    "usb-audio-sync-warning",
                ],
                evidence_basis="LPM Outdoor Concert setup names Behringer U-Phoria.",
            ),
            DeviceContract(
                contract_id="portable-wifi-hotspot",
                label="WiFi/hotspot network",
                device_class="network",
                integration_surface="network flake injection and egress reachability checks",
                proof_level="mocked",
                capabilities=[
                    "latency injection",
                    "dropout injection",
                    "egress retry/recovery proof",
                ],
                failure_modes=[
                    "hotspot disconnect",
                    "high latency",
                    "DNS failure",
                    "Castr unreachable",
                ],
                required_checks=[
                    "wifi-latency-injection",
                    "wifi-dropout",
                    "dns-failure",
                    "castr-unreachable",
                    "youtube-destination-confirmed",
                    "egress-retry-recovery",
                ],
                evidence_basis="LPM Outdoor Concert setup lists WiFi hotspot as optional.",
                station_device_evidence_required=False,
            ),
        ],
        egress_destinations=["Castr", "LPM YouTube stream"],
        claims=[
            "CivicCast can model the portable livestreaming path without assuming DeckLink or PTZ.",
            "Portable check-catalog coverage must include no-DeckLink and no-PTZ behavior as passing states.",
        ],
        not_claimed=[
            "No claim that the capture device is Elgato unless LPM confirms it.",
            "No claim that wired-studio latency assumptions apply to the field kit.",
        ],
    )

    digitization = LabTopologyProfile(
        profile_id="digitization-obs",
        label="Digitization OBS",
        priority=3,
        purpose=(
            "OBS recording workflow used by the digitization room. This is the natural "
            "LPM-used OBS proof target, but not the main live-switching path."
        ),
        sources=[
            _source(
                "lpm-digitization-studio-wiki",
                "LPM Digitization Studio wiki",
                "https://wiki.longmontpublicmedia.org/makerspace/studios/digitization-studio",
                "direct-lpm-doc",
                [
                    "digitization-obs",
                    "digitization-elgato-video-capture",
                    "digitization-davinci-resolve",
                    "digitization-audacity",
                ],
            ),
        ],
        network_profile="Local recording workflow; no live switcher network dependency required.",
        required_absences=["vMix", "DeckLink card", "AIDA/PTZ target"],
        devices=[
            DeviceContract(
                contract_id="digitization-obs-studio",
                label="OBS Studio",
                device_class="obs",
                integration_surface="obs-websocket 5.x",
                proof_level="mocked",
                capabilities=[
                    "connect/auth",
                    "protocol-version refusal",
                    "scene/source state",
                    "recording state",
                    "event subscription",
                ],
                failure_modes=[
                    "obs-websocket disabled",
                    "wrong password",
                    "protocol mismatch",
                    "source missing",
                    "OBS restart",
                ],
                required_checks=[
                    "obs-websocket-5-contract",
                    "obs-websocket-disabled",
                    "obs-wrong-password",
                    "obs-protocol-mismatch",
                    "obs-source-missing",
                    "obs-source-removed",
                    "obs-recording-state",
                    "obs-restart",
                    "obs-event-subscription",
                ],
                evidence_basis="LPM Digitization Studio wiki lists OBS Studio.",
            ),
            DeviceContract(
                contract_id="digitization-elgato-video-capture",
                label="Elgato Video Capture",
                device_class="usb-capture",
                integration_surface="Windows capture-device enumeration and OBS source identity",
                proof_level="mocked",
                capabilities=["device present/missing", "source identity", "recording input proof"],
                failure_modes=["capture device absent", "deck not playing", "OBS source removed"],
                required_checks=[
                    "usb-capture-present",
                    "usb-capture-absent",
                    "usb-capture-deck-not-playing",
                    "elgato-obs-source-removed",
                    "local-recording-evidence",
                ],
                evidence_basis="LPM Digitization Studio wiki lists Elgato Video Capture.",
            ),
        ],
        egress_destinations=["local recording"],
        claims=[
            "CivicCast can represent OBS contract requirements against an LPM-used workflow.",
        ],
        not_claimed=[
            "Digitization OBS proof is not a live production-switching proof by itself.",
        ],
    )

    return {profile.profile_id: profile for profile in (fixed, portable, digitization)}


def validate_lpm_lab_profiles(
    profiles: dict[TopologyId, LabTopologyProfile] | None = None,
) -> list[str]:
    """Return human-readable contract issues; empty means the profile set is sane."""

    if profiles is None:
        profiles = build_lpm_lab_profiles()
    issues: list[str] = []
    expected = {"fixed-studio-livestreaming", "portable-field-kit", "digitization-obs"}
    if set(profiles) != expected:
        issues.append(f"Expected exactly {sorted(expected)} profiles, got {sorted(profiles)}.")

    body = "\n".join(profile.model_dump_json() for profile in profiles.values()).lower()
    forbidden_secret_fragments = ("admin/admin", "password=", "secret=", "token=")
    for fragment in forbidden_secret_fragments:
        if fragment in body:
            issues.append(f"LPM Lab profiles contain forbidden secret-looking text: {fragment}")

    for profile in profiles.values():
        if not profile.sources:
            issues.append(f"{profile.profile_id} must have at least one structured source.")
        for source in profile.sources:
            if not source.claim_ids:
                issues.append(f"{profile.profile_id} source {source.source_id} has no claim IDs.")
        for device in profile.devices:
            if not device.required_checks:
                issues.append(f"{profile.profile_id}/{device.contract_id} has no required checks.")
            duplicate_checks = {
                check for check in device.required_checks if device.required_checks.count(check) > 1
            }
            for check in sorted(duplicate_checks):
                issues.append(f"{profile.profile_id}/{device.contract_id} repeats check {check}.")

    fixed = profiles.get("fixed-studio-livestreaming")
    if fixed and "fixed-decklink-duo-2-channels-2-3-4" not in fixed.device_ids:
        issues.append("Fixed studio profile must carry the DeckLink Duo 2 channel inference.")

    portable = profiles.get("portable-field-kit")
    if portable:
        portable_classes = {device.device_class for device in portable.devices}
        if "decklink" in portable_classes:
            issues.append("Portable field kit must not require DeckLink.")
        if any("ptz" in cls for cls in portable_classes):
            issues.append("Portable field kit must not require PTZ.")
        if not {"Castr", "LPM YouTube stream"}.issubset(set(portable.egress_destinations)):
            issues.append("Portable field kit must include Castr and LPM YouTube egress.")

    digitization = profiles.get("digitization-obs")
    if digitization and "digitization-obs-studio" not in digitization.device_ids:
        issues.append("Digitization profile must carry OBS Studio as the proof target.")

    return issues


__all__ = [
    "DeviceContract",
    "LabTopologyProfile",
    "ProofLevel",
    "SourceRecord",
    "SourceType",
    "TopologyId",
    "build_lpm_lab_profiles",
    "validate_lpm_lab_profiles",
]
