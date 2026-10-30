# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S3 commissioning wizard: the post-first-admin cable/headend commissioning flow.

Extends the existing 7-step first-run installer wizard with the 4
commissioning steps S3 §1a defines (screens 8-11): first-run cable checks,
channel output setup, output proof, and the final commissioning report.
Screens 1-7 (profile/hardware/storage/operator-account/publish-targets/
models/health) are unchanged and owned by
:func:`civiccast.installer.service.build_first_run_plan`; this module picks
up immediately after "operator account" exists.

State is a **computed report + a small persisted state machine**, not a
DB table (S3 §3, RECONCILIATION D-table): the per-step results ride
station-state JSON (:mod:`civiccast.installer.station_state`), exactly like
the S13 AI-model first-run seed, so a restart mid-commissioning resumes
from whatever step last completed rather than losing progress.

Each step function is a thin orchestration layer over primitives that
already exist and are independently tested: S1's ``StationBoxProfile``
(hardware/engine/clock/backup/TSDuck readiness), the installer's durable
storage health probes, S2's ``HeadendProfile`` catalog,
and the egress module's TSDuck compliance prober. This module does not
re-implement any of those checks — it aggregates their verdicts into the
commissioning shape and adds the two genuinely new pieces: channel-setup
validation and the bounded test-pattern-to-UDP output proof.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.egress.compliance import ComplianceProbeResult
from civiccast.egress.headend import HeadendProfile, get_headend_profile
from civiccast.egress.models import EgressConfig, EgressSinkSpec
from civiccast.egress.sdi_relay import SdiReadiness
from civiccast.installer.cea708_verification import Cea708VerificationResult
from civiccast.installer.models import DeploymentProfile
from civiccast.platform.station_box_profile import (
    GstRuntimeSource,
    StationBoxProfile,
    probe_station_box_profile,
)

CommissioningCheckStatus = Literal["pass", "fail", "warning", "skipped"]
OutputFormat = Literal["720p30", "1080i60", "1080p30", "SD480i60"]
FillPolicy = Literal["slate", "loop", "silence"]
TestPattern = Literal["bars", "live", "slate"]
ProofVerdict = Literal["pass", "fail", "partial", "not-run"]

#: Human-readable label for ``EngineReadiness.runtime_source`` (S1
#: ``station_box_profile``) so Screen 8's detail text tells the operator
#: WHICH install a passing (or failing) probe actually found -- "bundled"
#: (the CivicCast-shipped runtime) vs "system-path" (a developer/CI/system
#: GStreamer that happens to be on PATH, not the shipped runtime) vs
#: "unavailable" (found nowhere).
_RUNTIME_SOURCE_LABEL: dict[GstRuntimeSource, str] = {
    "bundled": "the installed CivicCast runtime",
    "system-path": "a system PATH install, not the CivicCast-shipped runtime",
    "unavailable": "found in neither the installed CivicCast runtime nor system PATH",
}

_NOT_CLAIMED_BOUNDARY = (
    "The output proof drives a bounded ffmpeg SMPTE-bars+tone generator at the "
    "configured muxrate onto the channel's UDP-TS destination and runs the "
    "existing TSDuck compliance probe concurrently; it is a headend "
    "connectivity/format proof, not a physical SDI/DeckLink hardware proof "
    "(that remains rung 3, MASTER §13.2, gated on real DeckLink hardware)."
)

# ---------------------------------------------------------------------------
# §3 Models
# ---------------------------------------------------------------------------


class CommissioningCheckItem(BaseModel):
    """One check from the Screen 8 first-run cable checks list."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    status: CommissioningCheckStatus
    detail: str = ""
    next_step: str = ""


class CommissioningCheckReport(BaseModel):
    """All Screen 8 checks collected."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    station_name: str = ""
    checks: list[CommissioningCheckItem] = Field(default_factory=list)
    ready: bool
    blockers: list[str] = Field(default_factory=list)
    support_bundle_path: str | None = None


class ChannelCommissioningSetup(BaseModel):
    """Operator's Screen 9 channel choices."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    channel_name: Annotated[str, Field(min_length=1, max_length=120)]
    output_format: OutputFormat
    headend_profile_id: Annotated[str, Field(min_length=1, max_length=80)]
    destination: Annotated[str, Field(min_length=1, max_length=500)]
    muxrate_kbps: Annotated[int, Field(gt=0)] | None = None
    sdi_device: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    fill_policy: FillPolicy = "slate"
    emergency_slate_asset_id: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    cea708_passthrough: bool = False
    watch_folder_path: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class OutputProofSettings(BaseModel):
    """Screen 10 controls for one output-proof run."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    test_pattern: TestPattern = "bars"
    duration_seconds: Annotated[int, Field(gt=0, le=1800)] = 60
    output_directory: str | None = None
    capture_raw_ts: bool = False


class CommissioningProofRun(BaseModel):
    """Screen 10 proof result."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    proof_id: Annotated[str, Field(min_length=1, max_length=80)]
    started_at: datetime
    ended_at: datetime | None = None
    test_pattern: TestPattern
    compliance_probe_result: ComplianceProbeResult | None = None
    sdi_device_status: SdiReadiness | None = None
    cea708_verified: bool | None = None
    verdict: ProofVerdict
    blockers: list[str] = Field(default_factory=list)
    detail: str = ""
    raw_ts_path: str | None = None
    not_claimed: list[str] = Field(default_factory=lambda: [_NOT_CLAIMED_BOUNDARY])


class CommissioningReport(BaseModel):
    """Screen 11 final handoff."""

    model_config = ConfigDict(extra="forbid")

    station_name: str
    channel_name: str
    headend_profile_id: str
    output_format: OutputFormat
    sdi_device: str | None = None
    completed_at: datetime
    first_run_checks: CommissioningCheckReport
    channel_setup: ChannelCommissioningSetup
    proof_run: CommissioningProofRun
    ready_for_broadcast: bool
    next_steps: list[str] = Field(default_factory=list)
    support_bundle_path: str | None = None


class CommissioningState(BaseModel):
    """Resumable per-step commissioning progress (station-state JSON, no DB)."""

    model_config = ConfigDict(extra="forbid")

    first_run_checks: CommissioningCheckReport | None = None
    channel_setup: ChannelCommissioningSetup | None = None
    proof_run: CommissioningProofRun | None = None
    report: CommissioningReport | None = None


class ChannelSetupValidationError(ValueError):
    """Raised when Screen 9's channel setup fails validation."""


# ---------------------------------------------------------------------------
# Screen 8: first-run cable checks (S3 §6, 11 checks)
# ---------------------------------------------------------------------------


def run_first_run_cable_checks(
    *,
    deployment_profile: DeploymentProfile = "public-meetings",
    station_name: str = "",
    box_profile: StationBoxProfile | None = None,
    storage_status: object | None = None,
    event_bus_ready: bool | None = None,
) -> CommissioningCheckReport:
    """Run the Screen 8 first-run cable checks (S3 §6, fail-closed on Continue).

    Every check reuses an existing, independently-tested primitive rather
    than re-probing: ``box_profile`` (S1 ``StationBoxProfile``) supplies
    os/disk/engine/DeckLink/TSDuck/clock/backup; ``storage_status``/
    ``event_bus_ready`` are injectable for tests; the event bus is the
    in-process broker (ADR 0024 removed NATS), so its default probe is the
    broker factory constructing successfully.
    """

    if box_profile is None:
        # Keep backup setup/proof separate from durable-database readiness.
        from civiccast.installer.service import build_backup_status

        profile = probe_station_box_profile(
            deployment_profile=deployment_profile,
            backup_status=build_backup_status(),
        )
    else:
        profile = box_profile
    cable_tier_targeted = deployment_profile == "peg-cable"
    premium_tier_targeted = profile.qualified_engine_tier.qualifies_for == "premium-cg"

    checks: list[CommissioningCheckItem] = []

    # 1. os_version
    hw = profile.hardware
    os_ok = hw.os.kind in ("windows", "linux", "macos")
    checks.append(
        CommissioningCheckItem(
            id="os_version",
            label="Operating system",
            status="pass" if os_ok else "warning",
            detail=f"{hw.os.system} {hw.os.release} ({hw.os.kind})",
            next_step=""
            if os_ok
            else "Run CivicCast on native Windows 10+, Ubuntu 22.04+, or RHEL 8+.",
        )
    )

    # 2. disk_available_gb
    disk_ok = hw.disk.free_gb >= 100
    checks.append(
        CommissioningCheckItem(
            id="disk_available_gb",
            label="Disk space",
            status="pass" if disk_ok else "fail",
            detail=f"{hw.disk.free_gb} GB free on {hw.disk.path}",
            next_step=""
            if disk_ok
            else "Free at least 100GB on the media volume before commissioning.",
        )
    )

    # 3. gstreamer_engine
    engine = profile.engine
    engine_ok = engine.gstreamer_present and profile.qualified_engine_tier.base_ok
    checks.append(
        CommissioningCheckItem(
            id="gstreamer_engine",
            label="GStreamer playout engine",
            status="pass" if engine_ok else "fail",
            detail=(
                f"GStreamer {engine.gstreamer_version} ({_RUNTIME_SOURCE_LABEL[engine.runtime_source]}), "
                f"tier {profile.qualified_engine_tier.qualifies_for}"
                if engine.gstreamer_present
                else "GStreamer runtime not detected: checked the installed CivicCast "
                "runtime and system PATH, found neither."
            ),
            next_step="" if engine_ok else engine.next_step,
        )
    )

    # 4. decklink_sdi (SDI tier only) -- optional hardware: a peg-cable
    # station commonly delivers IP/ASI to the headend rather than physical
    # SDI (validate_channel_commissioning_setup already treats an SDI device
    # as a per-channel opt-in, not a station-wide requirement), so its
    # absence is a WARNING, never a "fail" that locks Screen 9 for every
    # channel type on a station that will never plug in a capture card.
    if not cable_tier_targeted:
        checks.append(
            CommissioningCheckItem(
                id="decklink_sdi",
                label="DeckLink / BMD Desktop Video SDK",
                status="skipped",
                detail="Not applicable outside the peg-cable deployment profile.",
            )
        )
    else:
        sdi_ok = engine.decklink.bmd_sdk_present
        checks.append(
            CommissioningCheckItem(
                id="decklink_sdi",
                label="DeckLink / BMD Desktop Video SDK",
                status="pass" if sdi_ok else "warning",
                detail=(
                    "decklinkvideosink + BMD SDK detected"
                    if sdi_ok
                    else "Not installed (optional — only required for a channel that outputs via SDI)"
                ),
                next_step="" if sdi_ok else engine.next_step,
            )
        )

    # 5. tsduck (optional: a TSDuck compliance probe is a nice-to-have on
    # commissioning, not required to configure or air a channel)
    tsduck_ok = profile.tsduck.installed
    checks.append(
        CommissioningCheckItem(
            id="tsduck",
            label="TSDuck",
            status="pass" if tsduck_ok else "warning",
            detail=(profile.tsduck.version or "installed")
            if tsduck_ok
            else "Not installed (optional)",
            next_step="" if tsduck_ok else profile.tsduck.install_hint,
        )
    )

    # 6. db
    if storage_status is None:
        from civiccast.installer.storage import durable_storage_status

        storage_status = durable_storage_status()
    db_ok = getattr(storage_status, "status", None) == "ready"
    checks.append(
        CommissioningCheckItem(
            id="db",
            label="Database",
            status="pass" if db_ok else "fail",
            detail=getattr(storage_status, "operator_message", "") or "",
            next_step=""
            if db_ok
            else "Open Setup and prepare durable storage before commissioning.",
        )
    )

    # 7. services (in-process event bus; NATS was removed by ADR 0024)
    if event_bus_ready is None:
        try:
            from civiccast.platform.broker import get_broker_client

            event_bus_ready = get_broker_client() is not None
        except Exception:
            event_bus_ready = False
    checks.append(
        CommissioningCheckItem(
            id="services",
            label="Event bus (in-process)",
            status="pass" if event_bus_ready else "warning",
            detail="In-process event bus available"
            if event_bus_ready
            else "In-process event bus could not be constructed.",
            next_step=""
            if event_bus_ready
            else "The in-process event bus failed to construct; check the application logs and retry.",
        )
    )

    # 8. backup
    backup_ok = profile.backup_destination.configured and (
        profile.backup_destination.reachable is not False
    )
    checks.append(
        CommissioningCheckItem(
            id="backup",
            label="Backup destination",
            status="pass" if backup_ok else "warning",
            detail=profile.backup_destination.destination or "Not configured",
            next_step="" if backup_ok else "Configure a backup destination in the installer.",
        )
    )

    # 9. timezone
    tz_ok = profile.clock.timezone.lower() not in ("utc", "coordinated universal time")
    checks.append(
        CommissioningCheckItem(
            id="timezone",
            label="Timezone",
            status="pass" if tz_ok else "warning",
            detail=profile.clock.timezone,
            next_step="" if tz_ok else "Set the station's real local timezone in Station Profile.",
        )
    )

    # 10. release_integrity — honest not-run unless the operator supplied an
    # artifact + sidecar to verify (that verification is install-time work;
    # a running service has no artifact path to check against by default).
    checks.append(
        CommissioningCheckItem(
            id="release_integrity",
            label="Release integrity",
            status="skipped",
            detail=f"Running CivicCast {hw.civiccast_version}; package signature not re-verified at runtime.",
            next_step="Verify the release artifact's signature at install time (civiccast package verify).",
        )
    )

    # 11. caspar_cg (premium-CG tier only)
    if not premium_tier_targeted:
        checks.append(
            CommissioningCheckItem(
                id="caspar_cg",
                label="CasparCG co-process",
                status="skipped",
                detail="Optional premium-CG tier not targeted.",
            )
        )
    else:
        checks.append(
            CommissioningCheckItem(
                id="caspar_cg",
                label="CasparCG co-process",
                status="warning",
                detail="CasparCG AMCP reachability is not probed by this check.",
                next_step="Confirm the CasparCG co-process is installed and reachable via AMCP.",
            )
        )

    blockers = [f"{check.label}: {check.detail}" for check in checks if check.status == "fail"]
    ready = not blockers

    return CommissioningCheckReport(
        generated_at=datetime.now(UTC),
        station_name=station_name,
        checks=checks,
        ready=ready,
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# Screen 9: channel output setup validation (S3 §6)
# ---------------------------------------------------------------------------


def validate_channel_commissioning_setup(
    setup: ChannelCommissioningSetup,
    *,
    box_profile: StationBoxProfile | None = None,
    port_reachable: Callable[[str], bool] | None = None,
) -> ChannelCommissioningSetup:
    """Validate Screen 9's channel setup (S3 §6). Raises on hard failures.

    Soft checks (port reachability) never block -- they are the operator's
    own headend network, unreachable during commissioning on a bench box is
    common and non-fatal (S3 §6: "TCP port reachable (non-fatal warning)").
    """

    profile: HeadendProfile | None = get_headend_profile(setup.headend_profile_id)
    if profile is None:
        raise ChannelSetupValidationError(
            f"Unknown headend profile {setup.headend_profile_id!r}. "
            "Choose one from the headend profile catalog."
        )

    box = box_profile or probe_station_box_profile()
    wants_sdi = setup.sdi_device is not None
    if wants_sdi and not box.engine.decklink.bmd_sdk_present:
        raise ChannelSetupValidationError(
            "An SDI device was selected but this station's GStreamer engine "
            "does not have the DeckLink/BMD Desktop Video SDK ready "
            f"({box.engine.next_step or 'see doctor --profile'})."
        )

    if setup.watch_folder_path is not None:
        path = Path(setup.watch_folder_path)
        if not path.is_dir():
            raise ChannelSetupValidationError(
                f"Watch-folder path {setup.watch_folder_path!r} does not exist or is not readable."
            )

    if port_reachable is not None:
        # Best-effort, non-fatal per S3 §6 -- callers may log/surface the
        # result but validation itself never raises on it.
        port_reachable(setup.destination)

    return setup


# ---------------------------------------------------------------------------
# Screen 10: output proof (S3 §6)
# ---------------------------------------------------------------------------

TestPatternRunner = Callable[[str, int, TestPattern, int], None]
ComplianceProber = Callable[[EgressConfig, int], ComplianceProbeResult]
CaptionVerifier = Callable[[int, int], Cea708VerificationResult]


def _default_test_pattern_runner(
    destination_uri: str, duration_seconds: int, pattern: TestPattern, muxrate_kbps: int
) -> None:
    """Drive a bounded SMPTE-bars+1kHz-tone (or slate) signal onto ``destination_uri``.

    Real ffmpeg lavfi generation (not a stub): ``smptebars``+``sine`` for
    ``bars``/``live`` fallback, a solid color for ``slate``, muxed to
    MPEG-TS at the channel's configured muxrate for exactly
    ``duration_seconds`` (bounded by ``-t`` and a hard subprocess timeout).
    This is the headend/format proof leg described in the module's
    ``_NOT_CLAIMED_BOUNDARY`` -- not a claim of physical SDI output.

    Routed through :func:`civiccast.stream._ffmpeg.run_ffmpeg` (ADR 0007:
    "All ffmpeg invocations ... go through run_ffmpeg; direct subprocess
    calls to ffmpeg from other modules are forbidden") rather than a local
    ``subprocess.run`` -- the wrapper also resolves and validates the
    ffmpeg binary, so there is no separate ``shutil.which`` check here.
    """

    from civiccast.stream._ffmpeg import FfmpegError, run_ffmpeg

    if pattern == "slate":
        video_filter = "color=c=0x1a2744:size=1280x720:rate=30"
        audio_filter = "anullsrc=sample_rate=48000:channel_layout=stereo"
    else:
        video_filter = "smptebars=size=1280x720:rate=30"
        audio_filter = "sine=frequency=1000:sample_rate=48000"

    video_kbps = max(muxrate_kbps - 128, 500)
    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        video_filter,
        "-f",
        "lavfi",
        "-i",
        audio_filter,
        "-t",
        str(duration_seconds),
        "-c:v",
        "mpeg2video",
        "-b:v",
        f"{video_kbps}k",
        "-c:a",
        "mp2",
        "-b:a",
        "128k",
        "-f",
        "mpegts",
        "-muxrate",
        f"{muxrate_kbps * 1000}",
        destination_uri,
    ]
    result = run_ffmpeg(args, timeout=duration_seconds + 15)
    if result.returncode != 0:
        raise FfmpegError(
            f"ffmpeg test-pattern generation exited {result.returncode}.",
            result.returncode,
            result.stderr,
        )


def _extract_muxrate_kbps(sink: EgressSinkSpec) -> int:
    """The sink's configured muxrate in kbps, defaulting to 4000 when unset.

    Reuses :func:`civiccast.egress.compliance._muxrate_kbps` rather than
    re-parsing the ``-muxrate <N>k`` extra-arg format (``apply_headend_profile``
    in ``headend.py`` writes it as e.g. ``"4000k"``, not raw bps) so the two
    parsers can never silently drift apart.
    """

    from civiccast.egress.compliance import _muxrate_kbps

    return _muxrate_kbps(sink) or 4000


def _default_compliance_prober(config: EgressConfig, seconds: int) -> ComplianceProbeResult:
    from civiccast.egress.compliance import run_compliance_probe
    from civiccast.egress.router import get_egress_work_dir

    return run_compliance_probe(config, seconds=seconds, work_dir=get_egress_work_dir())


def _default_caption_verifier(duration_seconds: int, muxrate_kbps: int) -> Cea708VerificationResult:
    """The real CEA-708 embed-through-decode-back check (S11), bounded to a short
    window regardless of the overall proof's ``duration_seconds`` -- it only needs
    to embed and decode one deterministic test caption, not run for the full
    output-proof window."""
    from civiccast.egress.router import get_egress_work_dir
    from civiccast.installer.cea708_verification import (
        DEFAULT_EMBED_DURATION_SECONDS,
        verify_cea708_decode_back,
    )

    bounded_duration = max(1, min(duration_seconds, DEFAULT_EMBED_DURATION_SECONDS))
    return verify_cea708_decode_back(
        duration_seconds=bounded_duration,
        muxrate_kbps=muxrate_kbps,
        work_dir=get_egress_work_dir(),
    )


def run_output_proof(
    settings: OutputProofSettings,
    *,
    config: EgressConfig,
    test_pattern_runner: TestPatternRunner | None = None,
    compliance_prober: ComplianceProber | None = None,
    caption_verifier: CaptionVerifier | None = None,
    box_profile: StationBoxProfile | None = None,
    cea708_expected: bool = False,
    proof_id: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> CommissioningProofRun:
    """Run the Screen 10 output proof (S3 §6): test pattern + concurrent TSDuck probe,
    then (when ``cea708_expected``) a real CEA-708 embed-through-decode-back check.

    Drives the bounded test-pattern generator and the TSDuck compliance
    probe concurrently (mirrors a real headend acceptance run), for the
    same bounded window. Fail-closed: any exception from any leg is
    captured as a blocker rather than silently producing a fabricated
    "pass". When CEA-708 passthrough was requested (S11/D12), this now runs
    :func:`civiccast.installer.cea708_verification.verify_cea708_decode_back`
    (real embed through the product's GStreamer caption leg, then decode-back)
    AFTER the main test-pattern/compliance window, and reports the actual
    True/False result -- ``cea708_verified`` stays ``None`` only when the
    verifier itself could not run (e.g. it raised), never a fabricated claim.
    """

    clock = now or (lambda: datetime.now(UTC))
    started_at = clock()
    run_id = proof_id or f"proof_{int(started_at.timestamp())}_{settings.channel_id}"
    runner = test_pattern_runner or _default_test_pattern_runner
    prober = compliance_prober or _default_compliance_prober

    from civiccast.egress.compliance import _udp_ts_sink

    blockers: list[str] = []
    detail_parts: list[str] = []
    compliance_result: ComplianceProbeResult | None = None

    try:
        sink = _udp_ts_sink(config)
    except ValueError as exc:
        blockers.append(str(exc))
        sink = None

    muxrate_kbps = _extract_muxrate_kbps(sink) if sink is not None else 4000

    if sink is not None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pattern_future = pool.submit(
                runner, sink.uri, settings.duration_seconds, settings.test_pattern, muxrate_kbps
            )
            probe_future = pool.submit(prober, config, settings.duration_seconds)
            try:
                pattern_future.result()
            except Exception as exc:
                blockers.append(f"Test pattern generation failed: {exc}")
            try:
                compliance_result = probe_future.result()
            except Exception as exc:
                blockers.append(f"TSDuck compliance probe failed: {exc}")

    if compliance_result is not None:
        detail_parts.append(f"TSDuck verdict: {compliance_result.verdict}")
        if compliance_result.verdict == "fail":
            blockers.append(f"TSDuck verdict failed: {compliance_result.detail}")

    sdi_status = box_profile.sdi if box_profile is not None else None

    cea708_verified: bool | None = None
    if cea708_expected:
        verifier = caption_verifier or _default_caption_verifier
        try:
            caption_result = verifier(settings.duration_seconds, muxrate_kbps)
        except Exception as exc:
            # The verifier itself blew up (not a clean PASS/FAIL) -- this is the
            # one case that still reports None: we could not even attempt a real
            # check, so we never claim True or False either way.
            caption_result = None
            blockers.append(f"CEA-708 decode-back verification could not run: {exc}")
        if caption_result is not None:
            cea708_verified = caption_result.verified
            if not caption_result.verified:
                blockers.append(f"CEA-708 decode-back: {caption_result.detail}")

    verdict: ProofVerdict
    if sink is None or compliance_result is None:
        # Could not even attempt the probe (no udp-ts sink, or the probe leg
        # itself errored) -- always a hard fail, never soft-partial.
        verdict = "fail"
    elif compliance_result.verdict == "fail":
        # The headend/format proof itself failed -- fail, regardless of
        # whether other (e.g. CEA-708) blockers are also present.
        verdict = "fail"
    elif blockers:
        # Compliance passed but some other leg is degraded/unverified
        # (e.g. CEA-708 passthrough requested but not decode-checked).
        verdict = "partial"
    elif compliance_result.verdict == "pass":
        verdict = "pass"
    else:
        verdict = "not-run"

    return CommissioningProofRun(
        channel_id=settings.channel_id,
        proof_id=run_id,
        started_at=started_at,
        ended_at=clock(),
        test_pattern=settings.test_pattern,
        compliance_probe_result=compliance_result,
        sdi_device_status=sdi_status,
        cea708_verified=cea708_verified,
        verdict=verdict,
        blockers=blockers,
        detail="; ".join(detail_parts),
    )


# ---------------------------------------------------------------------------
# Screen 11: commissioning report
# ---------------------------------------------------------------------------


def build_commissioning_report(
    *,
    station_name: str,
    first_run_checks: CommissioningCheckReport,
    channel_setup: ChannelCommissioningSetup,
    proof_run: CommissioningProofRun,
    support_bundle_path: str | None = None,
) -> CommissioningReport:
    """Aggregate the 3 prior steps into the final Screen 11 report (S3 §6)."""

    next_steps: list[str] = []
    next_steps.extend(check.next_step for check in first_run_checks.checks if check.next_step)
    next_steps.extend(proof_run.blockers)

    ready_for_broadcast = (
        first_run_checks.ready
        and proof_run.verdict in ("pass", "partial")
        and not proof_run.blockers
    )

    return CommissioningReport(
        station_name=station_name,
        channel_name=channel_setup.channel_name,
        headend_profile_id=channel_setup.headend_profile_id,
        output_format=channel_setup.output_format,
        sdi_device=channel_setup.sdi_device,
        completed_at=datetime.now(UTC),
        first_run_checks=first_run_checks,
        channel_setup=channel_setup,
        proof_run=proof_run,
        ready_for_broadcast=ready_for_broadcast,
        next_steps=next_steps,
        support_bundle_path=support_bundle_path,
    )
