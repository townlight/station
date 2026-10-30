# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""StationBoxProfile — S1 cable/PEG appliance-readiness capability model.

Extends :func:`civiccast.platform.hardware.probe` from a streaming-tier GPU
probe into a full cable/PEG appliance-readiness report: playout-engine
(GStreamer, S15) prerequisite detection per tier, DeckLink/BMD Desktop
Video SDK and NDI SDK readiness, ffmpeg feature flags (transitional /
utility-path only — the box's playout engine is GStreamer, not per-segment
ffmpeg), system-clock sanity, network context, and the derived
AI-default / PEG-readiness / cable-OS verdicts.

``StationBoxProfile`` is a **computed, in-memory report** (spec S1 §3):
no DB table, no Alembic migration. Every dimension is probed honestly —
an absent binary/SDK/plugin reports a concrete ``next_step``, never a
faked pass (S1 §7-9 honesty boundary). Where a genuine hardware probe is
not possible in software (physical DeckLink card presence, true OpenGL
context version), the probe documents the heuristic it uses and fails
closed rather than inventing a capability.

Two proof-boundary notes, spelled out here so they are not lost in code:

* ``EngineReadiness.decklink.card_present`` is a best-effort software
  heuristic (a ``gst-device-monitor-1.0`` scan), not a hardware proof.
  Physical SDI proof is rung 3 (MASTER §13.2) and is gated on real
  DeckLink hardware landing on a tester — this module never claims it.
* ``EngineReadiness.opengl_45`` is inferred from NVIDIA GPU presence
  (every NVML-visible NVIDIA GPU CivicCast supports ships OpenGL 4.6+
  drivers) rather than a genuine GL-context query, because this process
  has no GL context to query. Documented as a heuristic, never reported
  as a measured fact.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.egress.compliance import TsduckStatus, locate_tsduck
from civiccast.egress.sdi_relay import SdiReadiness, cached_check_sdi_runtime
from civiccast.egress.sdi_relay import SdiRelaySettings as _SdiRelaySettings
from civiccast.installer.models import BackupStatus, DeploymentProfile
from civiccast.platform.hardware import GPUInfo, HardwareProbe, OSKind, probe
from civiccast.stream._ffmpeg import FfmpegResult, run_ffmpeg

SCHEMA_VERSION = 1

EngineTier = Literal["base", "sdi-broadcast", "premium-cg"]
HardwareEncoder = Literal["nvenc", "vaapi", "qsv", "none"]
NtpSyncState = Literal["synced", "unsynced", "unknown"]
GstRuntimeSource = Literal["bundled", "system-path", "unavailable"]
CableOsVerdictKind = Literal["single-windows-pc-ok", "native-linux-recommended", "soak-pending"]
PegReadinessColor = Literal["green", "yellow", "red"]
AiDefaultBasis = Literal["ram-12b", "ram-e4b", "forced-cpu", "cpu-only"]

ProcRunner = Callable[[list[str]], FfmpegResult]

# ---------------------------------------------------------------------------
# Leaf models (S1 spec §3)
# ---------------------------------------------------------------------------


class DeckLinkEngineRef(BaseModel):
    """BMD Desktop Video SDK readiness the GStreamer ``decklinkvideosink`` needs."""

    model_config = ConfigDict(extra="forbid")

    card_present: bool
    bmd_sdk_present: bool
    sdk_version: str | None = None


class NdiSdkRef(BaseModel):
    """NDI SDK 5/6 readiness the GStreamer ``ndisink`` needs."""

    model_config = ConfigDict(extra="forbid")

    sdk_present: bool
    sdk_version: str | None = None


class EngineReadiness(BaseModel):
    """GStreamer playout-engine prerequisites per S15 tier."""

    model_config = ConfigDict(extra="forbid")

    gstreamer_present: bool
    gstreamer_version: str | None = None
    required_plugins_present: bool
    missing_plugins: list[str] = Field(default_factory=list)
    opengl_45: bool
    hw_encoder: HardwareEncoder
    decklink: DeckLinkEngineRef
    ndi_sdk: NdiSdkRef
    native_os: bool
    next_step: str = ""
    #: Which install a default (non-injected) probe run actually resolved to
    #: -- "bundled" (the CivicCast-shipped runtime, resolved by absolute path,
    #: never PATH), "system-path" (a PATH-found dev/CI/system-wide GStreamer,
    #: NOT the shipped runtime -- a false pass on a developer's own machine
    #: would report this), or "unavailable" (found nowhere). Defaults to
    #: "unavailable" for callers/tests that construct this model directly
    #: with an injected runner, where the concept doesn't apply.
    runtime_source: GstRuntimeSource = "unavailable"


class EngineBlocker(BaseModel):
    """One unmet prerequisite for a higher engine tier."""

    model_config = ConfigDict(extra="forbid")

    tier: EngineTier
    reason: str
    next_step: str


class EngineTierVerdict(BaseModel):
    """Which S15 engine tier this box qualifies for (fail-closed)."""

    model_config = ConfigDict(extra="forbid")

    qualifies_for: EngineTier
    base_ok: bool
    sdi_broadcast_ok: bool
    premium_cg_ok: bool
    blockers: list[EngineBlocker] = Field(default_factory=list)


class FfmpegFeatureReport(BaseModel):
    """Transitional / utility-path ffmpeg feature flags (not the playout engine)."""

    model_config = ConfigDict(extra="forbid")

    detected: bool
    version: str | None = None
    supported: bool
    has_decklink: bool
    has_ndi: bool
    has_libx264: bool
    has_loudnorm: bool
    byo_sdi_binary: str | None = None
    next_step: str = ""


class ClockReport(BaseModel):
    """System clock sanity for a 24/7 program log."""

    model_config = ConfigDict(extra="forbid")

    timezone: str
    utc_offset_minutes: int
    system_time: datetime
    ntp_sync: NtpSyncState
    note: str = ""


class NetworkReport(BaseModel):
    """Network context; headend interface detail deferred to S2."""

    model_config = ConfigDict(extra="forbid")

    hostname: str
    primary_interface_up: bool
    headend_interface_hint: str | None = None


class BackupDestinationRef(BaseModel):
    """References the installer's own BackupStatus probe; not re-probed here."""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    reachable: bool | None = None
    destination: str | None = None
    last_probe_at: datetime | None = None


class ReleaseIdentityRef(BaseModel):
    """Release/build identity."""

    model_config = ConfigDict(extra="forbid")

    version: str
    package_verified: bool | None = None
    proof_state: str | None = None


class AiDefaultSelection(BaseModel):
    """Memory-adaptive summary/translate/caption default (S1 §6.1; S13 owns selection UI)."""

    model_config = ConfigDict(extra="forbid")

    summary_model: str
    translate_model: str
    caption_model: str
    basis: AiDefaultBasis
    detected_ram_gb: float
    rationale: str


class PegReadinessDimension(BaseModel):
    """One dimension of the PEG readiness roll-up."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    color: PegReadinessColor
    message: str
    next_step: str = ""


class PegReadinessRollup(BaseModel):
    """Fail-closed green/yellow/red roll-up across the cable + engine dimensions."""

    model_config = ConfigDict(extra="forbid")

    overall: PegReadinessColor
    dimensions: list[PegReadinessDimension] = Field(default_factory=list)


class CableOsVerdict(BaseModel):
    """Cable-grade OS verdict (MASTER §13.1, soak-pending until Scott decides)."""

    model_config = ConfigDict(extra="forbid")

    verdict: CableOsVerdictKind
    os_kind: OSKind
    rationale: str
    decision_ref: str = "MASTER §13.1"


class StationBoxProfile(BaseModel):
    """Full cable/PEG appliance-readiness report. Computed, never persisted."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    generated_at: datetime
    civiccast_version: str
    hardware: HardwareProbe
    system_ram_total_gb: float
    engine: EngineReadiness
    ffmpeg: FfmpegFeatureReport
    clock: ClockReport
    network: NetworkReport
    backup_destination: BackupDestinationRef
    release_identity: ReleaseIdentityRef
    sdi: SdiReadiness
    tsduck: TsduckStatus
    ndi_sdk: NdiSdkRef
    qualified_engine_tier: EngineTierVerdict
    ai_default: AiDefaultSelection
    peg_readiness: PegReadinessRollup
    cable_os_verdict: CableOsVerdict


# ---------------------------------------------------------------------------
# §6.1 Adaptive AI-default selection
# ---------------------------------------------------------------------------


def select_ai_defaults(hardware_probe: HardwareProbe) -> AiDefaultSelection:
    """RAM-and-GPU-keyed AI default table (S1 §6.1).

    Keys off **system RAM** (``hardware_probe.ram.total_gb``) AND GPU presence
    (``hardware_probe.gpu``). This is the *recommended default only*; S13 owns
    the operator's per-feature registry and pinned-selection surface, and
    this function must never overwrite an operator's pin (callers apply
    the override, not this function). Uses the exact shipped pull tags
    (``summary/ollama.py:19`` ``gemma4:e4b``, ``translate/ollama.py:17``
    ``translategemma:4b``) — a distinct vocabulary from S13's
    ``detect_summary_model_default`` registry-key form
    (``civiccast/ai_models/models.py``), which this function intentionally
    does not reuse: that function returns operator-selectable registry
    keys (``gemma4-12b-ollama``); this one reports the raw model tag for
    doctor/profile display.

    FIXED 2026-08-29 (field evidence, candidate #17): this table used to key
    12B purely off ``ram >= 16``, independent of GPU. That is the same bug
    S13's ``detect_summary_model_default`` had and was fixed for: on a 32GB
    CPU-only reference station, ``gemma4:12b`` took 366s to complete one
    summary generation and then failed twice more under realistic memory
    pressure, while ``gemma4:e4b`` completed every attempt (94-128s). This
    function now requires a real GPU before recommending 12B, matching S13's
    rule, so ``civiccast doctor``/profile output never recommends a model the
    box cannot reliably run — and never disagrees with what S13 actually
    resolves as the runtime default on the same hardware.
    """

    ram = hardware_probe.ram.total_gb
    has_gpu = hardware_probe.gpu is not None
    basis: AiDefaultBasis
    if has_gpu and ram >= 16:
        summary_model = "gemma4:12b"
        basis = "ram-12b"
        rationale = (
            "16GB+ system RAM and a detected GPU; 12B summary default "
            "(better long-context, MRCR 43.4 vs 25.4)."
        )
    elif not has_gpu and ram < 8:
        summary_model = "gemma4:e4b"
        basis = "forced-cpu"
        rationale = (
            "No GPU detected and <8GB system RAM; e4b on CPU, expect slow "
            "background summaries; box is under-provisioned."
        )
    elif not has_gpu:
        summary_model = "gemma4:e4b"
        basis = "cpu-only"
        rationale = (
            "No GPU detected; e4b summary default regardless of system RAM. "
            "Measured on a 32GB CPU-only reference station: 12B took 366s to "
            "complete a summary once and then failed twice more under "
            "realistic memory pressure, while e4b completed every attempt "
            "(94-128s). CPU-only inference throughput does not scale with "
            "RAM capacity."
        )
    else:
        summary_model = "gemma4:e4b"
        basis = "ram-e4b"
        rationale = "8-16GB system RAM; e4b summary default to stay within memory budget."
    return AiDefaultSelection(
        summary_model=summary_model,
        translate_model="translategemma:4b",
        caption_model="whisper-large-v3",
        basis=basis,
        detected_ram_gb=ram,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# §3 EngineReadiness — GStreamer engine prerequisite detection
# ---------------------------------------------------------------------------

#: Base-tier elements the SHIPPED engine genuinely cannot run without.
#:
#: FIXED 2026-08-30 (field evidence, candidate #19): this list used to mirror
#: S1 §6.5's original prose (`compositor`, `interpipesrc`/`interpipesink`,
#: `hlssink3`, `textoverlay`/`clockoverlay`, ...), which predates two recorded
#: product decisions, so Screen 8 failed EVERY correct install of the bundled
#: runtime:
#:
#: * S15's dated Stage-0 decision (2026-06-14) demoted GstInterpipe to an
#:   OPTIONAL future enhancement -- the shipping hot-swap mechanism is
#:   ``input-selector`` (`civiccast.egress.gst.engine` drives exactly that),
#:   and the RidgeRun interpipe plugin is not in the pinned upstream wheels
#:   at all (its source build is unauthorized, S15 §3/§9).
#: * The engine's HLS output is the supervised ffmpeg relay over ``udpsink``
#:   (`civiccast.egress.hls_relay` -- no ``hlssink*`` element ships), and the
#:   base slate deliberately avoids pango (`civiccast.egress.gst.bridge`,
#:   design D-S1-7), so ``hlssink3``/``textoverlay``/``clockoverlay``/
#:   ``compositor`` are not base prerequisites of the product that ships.
#:
#: Every factory here is in `civiccast.native.runtime_closure.REQUIRED_FACTORIES`
#: (asserted by tests/platform/test_station_box_profile.py), so this probe can
#: never demand an element the packaging closure does not guarantee.
_BASE_REQUIRED_PLUGINS: tuple[str, ...] = (
    "input-selector",
    "mpegtsmux",
    "udpsink",
    "srtsink",
    "videotestsrc",
    "audiotestsrc",
)

#: S15 CG-lite / native-HLS elements that are probed and honestly reported in
#: ``missing_plugins`` when absent (S1 §7-9 honesty boundary) but never gate
#: the base tier: the shipped product does not use them yet (see the
#: `_BASE_REQUIRED_PLUGINS` note above), and the bundled runtime does not
#: ship them today, so gating on them would fail every correct install.
_CG_HLS_OPTIONAL_PLUGINS: tuple[str, ...] = (
    "compositor",
    "textoverlay",
    "clockoverlay",
    "hlssink3",
)
_SDI_BROADCAST_EXTRA_PLUGINS: tuple[str, ...] = ("decklinkvideosink", "ndisink")
_PREMIUM_CG_EXTRA_PLUGINS: tuple[str, ...] = ("wpesrc",)
_HW_ENCODER_ELEMENTS: dict[HardwareEncoder, str] = {
    "nvenc": "nvh264enc",
    "vaapi": "vah264enc",
    "qsv": "qsvh264enc",
}

_GST_INSPECT_HINT = (
    "GStreamer runtime not found at the installed CivicCast runtime location or "
    "on PATH. Re-run the CivicCast installer's repair (native-app-payload "
    "component) to re-stage it, or for a manual/dev box, put gst-inspect-1.0 on "
    "PATH."
)

_GSTREAMER_DEPENDENCIES_SUBDIR = ("dependencies", "gstreamer")


def _resolve_bundled_gst_tool(tool_filename: str) -> tuple[Path, dict[str, str]] | None:
    """Resolve one bundled GStreamer CLI tool by ABSOLUTE path -- the same
    resolution the real playout engine uses to find its own runtime
    (``civiccast.native.station_runtime.load_native_station_environment`` /
    ``civiccast.native.gstreamer_runtime.installed_gstreamer_environment``),
    never a PATH lookup.

    Why this matters: the control-plane service runs as LocalSystem with the
    stock, installer-untouched PATH (see
    ``civiccast.native.supervisor.install_layout``'s module docstring) -- it
    never carries the bundled runtime's ``bin`` directory. A PATH-only probe
    (``shutil.which``) therefore reports "not detected" under the service even
    when a fully installed runtime is sitting on disk (field evidence,
    candidate #17: Screen 8 FAILed "GStreamer runtime not detected" against a
    clean station whose install tree was later confirmed complete). The same
    PATH-only probe also produces a FALSE PASS on a developer's own machine
    that happens to have a separate, user-installed GStreamer on PATH -- this
    resolver is tried FIRST specifically so a passing probe reports the
    product's own shipped runtime, not whatever else happens to be on PATH.

    Returns ``(executable_path, child_environment)`` when the bundled closure
    verifies at this install location AND the named tool is actually staged
    in it (not every GStreamer CLI tool ships in the closure -- e.g.
    ``gst-device-monitor-1.0.exe`` currently does not), else ``None`` so
    callers fall back to PATH themselves (dev boxes, CI, system-wide
    installs). Never raises: a missing/corrupt/partial bundled closure is a
    legitimate "not here, try PATH", not an error.
    """

    from civiccast.native.gstreamer_runtime import (
        GstreamerRuntimeError,
        installed_gstreamer_environment,
    )
    from civiccast.native.supervisor.install_layout import resolve_install_root

    try:
        install_root = resolve_install_root()
    except Exception:
        return None
    runtime_root = install_root / "runtime"
    if not runtime_root.joinpath(*_GSTREAMER_DEPENDENCIES_SUBDIR).is_dir():
        return None
    try:
        env = installed_gstreamer_environment(runtime_root, base_environment=os.environ)
    except (GstreamerRuntimeError, OSError):
        return None
    exe = runtime_root.joinpath(*_GSTREAMER_DEPENDENCIES_SUBDIR, "bin", tool_filename)
    if not exe.is_file():
        return None
    return exe, env


def _detect_gst_runtime_source() -> GstRuntimeSource:
    """Which install a default (non-injected) probe run would actually find,
    for honest Screen 8 reporting (S3 §6) -- distinguishes the product's own
    bundled runtime from a developer's/CI's own system GStreamer so an
    operator is never told "detected" without knowing which install that
    means. Pure path arithmetic, same cost profile as the resolvers it calls;
    safe to compute unconditionally."""

    if _resolve_bundled_gst_tool("gst-inspect-1.0.exe") is not None:
        return "bundled"
    if shutil.which("gst-inspect-1.0"):
        return "system-path"
    return "unavailable"


def _default_gst_inspect_runner(args: list[str]) -> FfmpegResult:
    bundled = _resolve_bundled_gst_tool("gst-inspect-1.0.exe")
    if bundled is not None:
        gst_inspect, env = bundled
    else:
        which = shutil.which("gst-inspect-1.0")
        if not which:
            raise FileNotFoundError(
                "gst-inspect-1.0 not found in the bundled CivicCast runtime or on PATH"
            )
        gst_inspect, env = Path(which), None
    completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
        [str(gst_inspect), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        env=env,
    )
    return FfmpegResult(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


def _plugin_present(run: ProcRunner, element: str) -> bool:
    try:
        result = run([element])
    except Exception:
        return False
    return result.returncode == 0


def _parse_gst_version(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if line.lower().startswith("gstreamer"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[-1]
    return None


def _default_device_monitor_runner(args: list[str]) -> FfmpegResult:
    bundled = _resolve_bundled_gst_tool("gst-device-monitor-1.0.exe")
    if bundled is not None:
        monitor, env = bundled
    else:
        which = shutil.which("gst-device-monitor-1.0")
        if not which:
            raise FileNotFoundError(
                "gst-device-monitor-1.0 not found in the bundled CivicCast runtime or on PATH"
            )
        monitor, env = Path(which), None
    completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
        [str(monitor), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
        env=env,
    )
    return FfmpegResult(
        returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
    )


def _probe_decklink_card_present(run: ProcRunner | None = None) -> bool:
    """Best-effort DeckLink presence heuristic (never a hardware proof).

    Scans ``gst-device-monitor-1.0 --types Video/Source`` output for a
    Decklink-named device. Fails closed to ``False`` whenever the tool is
    absent or errors — see the module docstring's proof-boundary note.
    """

    runner = run or _default_device_monitor_runner
    try:
        result = runner(["--types", "Video/Source"])
    except Exception:
        return False
    if result.returncode != 0:
        return False
    output = f"{result.stdout}\n{result.stderr}".lower()
    return "decklink" in output


def probe_engine_readiness(
    *,
    native_os: bool,
    gpu: GPUInfo | None,
    runner: ProcRunner | None = None,
    device_runner: ProcRunner | None = None,
    opengl_45_override: bool | None = None,
) -> EngineReadiness:
    """Probe the GStreamer engine prerequisites (S1 §3, §6.5).

    ``runner`` and ``device_runner`` are injectable for deterministic
    tests (mirrors ``cached_check_sdi_runtime``'s ``ffmpeg_runner``
    pattern) — never faked present in production; absence/errors always
    fail closed.
    """

    run = runner or _default_gst_inspect_runner
    gstreamer_present: bool
    gstreamer_version: str | None
    try:
        version_result = run(["--version"])
        gstreamer_present = version_result.returncode == 0
        gstreamer_version = _parse_gst_version(version_result.stdout) if gstreamer_present else None
    except Exception:
        gstreamer_present = False
        gstreamer_version = None

    missing: list[str] = []
    hw_encoder: HardwareEncoder = "none"
    if gstreamer_present:
        for plugin in (
            *_BASE_REQUIRED_PLUGINS,
            *_CG_HLS_OPTIONAL_PLUGINS,
            *_SDI_BROADCAST_EXTRA_PLUGINS,
        ):
            if not _plugin_present(run, plugin):
                missing.append(plugin)
        for encoder_kind, element in _HW_ENCODER_ELEMENTS.items():
            if _plugin_present(run, element):
                hw_encoder = encoder_kind
                break
    else:
        missing = list(_BASE_REQUIRED_PLUGINS)

    base_missing = [plugin for plugin in missing if plugin in _BASE_REQUIRED_PLUGINS]
    required_plugins_present = gstreamer_present and not base_missing

    decklink_sdk_present = gstreamer_present and "decklinkvideosink" not in missing
    ndi_sdk_present = gstreamer_present and "ndisink" not in missing
    card_present = _probe_decklink_card_present(device_runner)

    opengl_45 = opengl_45_override if opengl_45_override is not None else (gpu is not None)

    # Only meaningful when the DEFAULT runner actually ran (production path,
    # or a test exercising the default runner directly); an injected `runner`
    # bypasses `_default_gst_inspect_runner` entirely, so the source concept
    # doesn't apply there and the model's "unavailable" default stands.
    runtime_source: GstRuntimeSource = (
        _detect_gst_runtime_source() if runner is None else "unavailable"
    )

    next_step = _engine_next_step(
        gstreamer_present=gstreamer_present,
        missing=missing,
        decklink_sdk_present=decklink_sdk_present,
        ndi_sdk_present=ndi_sdk_present,
        native_os=native_os,
        runtime_source=runtime_source,
    )

    return EngineReadiness(
        gstreamer_present=gstreamer_present,
        gstreamer_version=gstreamer_version,
        required_plugins_present=required_plugins_present,
        missing_plugins=missing,
        opengl_45=opengl_45,
        hw_encoder=hw_encoder,
        decklink=DeckLinkEngineRef(
            card_present=card_present, bmd_sdk_present=decklink_sdk_present, sdk_version=None
        ),
        ndi_sdk=NdiSdkRef(sdk_present=ndi_sdk_present, sdk_version=None),
        native_os=native_os,
        next_step=next_step,
        runtime_source=runtime_source,
    )


def _engine_next_step(
    *,
    gstreamer_present: bool,
    missing: list[str],
    decklink_sdk_present: bool,
    ndi_sdk_present: bool,
    native_os: bool,
    runtime_source: GstRuntimeSource = "unavailable",
) -> str:
    if not gstreamer_present:
        return _GST_INSPECT_HINT
    base_missing = [plugin for plugin in missing if plugin in _BASE_REQUIRED_PLUGINS]
    if base_missing:
        if runtime_source == "bundled":
            # Every base-required element is guaranteed by the packaging
            # closure (runtime_closure.REQUIRED_FACTORIES), so a bundled
            # runtime missing one is an incomplete/corrupt install -- not
            # something an operator can "install a plugin" into.
            return (
                "The installed CivicCast GStreamer runtime is incomplete "
                f"(missing: {', '.join(base_missing)}). Re-run the CivicCast "
                "installer's repair (native-app-payload component) to re-stage it."
            )
        return f"Install gst-plugins-base/good/bad/rs for: {', '.join(base_missing)}."
    if not decklink_sdk_present:
        return (
            "Install gst-plugins-bad for decklinkvideosink + the Blackmagic "
            "Desktop Video SDK for the SDI/broadcast tier."
        )
    if not ndi_sdk_present:
        return "Install the NDI SDK 5/6 for ndisink."
    if not native_os:
        return (
            "No native OS detected — SDI requires native OS; PCIe DeckLink cannot passthrough WSL2."
        )
    return ""


def compute_engine_tier_verdict(engine: EngineReadiness) -> EngineTierVerdict:
    """Which S15 engine tier the box qualifies for (S1 §6.5, fail-closed)."""

    base_missing = [plugin for plugin in engine.missing_plugins if plugin in _BASE_REQUIRED_PLUGINS]
    base_ok = engine.gstreamer_present and not base_missing
    blockers: list[EngineBlocker] = []
    if not engine.gstreamer_present:
        blockers.append(
            EngineBlocker(
                tier="base",
                reason="GStreamer runtime not detected.",
                next_step=_GST_INSPECT_HINT,
            )
        )
    elif base_missing:
        blockers.append(
            EngineBlocker(
                tier="base",
                reason=f"Missing base plugin(s): {', '.join(base_missing)}.",
                # engine.next_step already carries the source-aware remedy
                # (bundled runtime -> installer repair; PATH runtime ->
                # install the plugin packages).
                next_step=engine.next_step
                or f"Install gst-plugins-base/good/bad/rs for: {', '.join(base_missing)}.",
            )
        )

    sdi_blockers: list[EngineBlocker] = []
    if not engine.decklink.bmd_sdk_present:
        sdi_blockers.append(
            EngineBlocker(
                tier="sdi-broadcast",
                reason="decklinkvideosink / BMD Desktop Video SDK not detected.",
                next_step=(
                    "Install the Blackmagic Desktop Video SDK and gst-plugins-bad "
                    "for decklinkvideosink."
                ),
            )
        )
    if not engine.ndi_sdk.sdk_present:
        sdi_blockers.append(
            EngineBlocker(
                tier="sdi-broadcast",
                reason="ndisink / NDI SDK not detected.",
                next_step="Install the NDI SDK 5/6 and the GStreamer ndi plugin.",
            )
        )
    if engine.hw_encoder == "none":
        sdi_blockers.append(
            EngineBlocker(
                tier="sdi-broadcast",
                reason="No hardware encoder (NVENC/VA-API/QSV) detected.",
                next_step=(
                    "Install a GPU with a supported hardware encoder; base-tier "
                    "boxes fall back to the bundled openh264enc CPU encoder."
                ),
            )
        )
    if not engine.opengl_45:
        sdi_blockers.append(
            EngineBlocker(
                tier="sdi-broadcast",
                reason="OpenGL 4.5 not detected.",
                next_step="Install a GPU/driver with OpenGL 4.5 support for GPU compositing.",
            )
        )
    if not engine.native_os:
        sdi_blockers.append(
            EngineBlocker(
                tier="sdi-broadcast",
                reason="Not running a native OS — PCIe DeckLink cannot passthrough WSL2.",
                next_step="Run CivicCast on native Windows or native Linux for the SDI tier.",
            )
        )
    sdi_broadcast_ok = base_ok and not sdi_blockers
    blockers.extend(sdi_blockers)

    premium_blockers: list[EngineBlocker] = []
    if not engine.opengl_45:
        premium_blockers.append(
            EngineBlocker(
                tier="premium-cg",
                reason="OpenGL 4.5 not detected (needed for the optional CasparCG co-process tier).",
                next_step="Install a GPU/driver with OpenGL 4.5 support.",
            )
        )
    # premium-cg is always optional (S1 §6.5): its blockers are reported but
    # never demote a box that already qualifies for a lower tier, and its
    # absence is never itself a failure.
    premium_cg_ok = sdi_broadcast_ok and not premium_blockers
    blockers.extend(premium_blockers)

    if premium_cg_ok:
        qualifies_for: EngineTier = "premium-cg"
    elif sdi_broadcast_ok:
        qualifies_for = "sdi-broadcast"
    else:
        # "base" is the floor label even when base_ok is False; base_ok +
        # blockers carry the honest state (S1 §6.5: "reports the highest
        # tier it genuinely meets and lists the blockers").
        qualifies_for = "base"

    return EngineTierVerdict(
        qualifies_for=qualifies_for,
        base_ok=base_ok,
        sdi_broadcast_ok=sdi_broadcast_ok,
        premium_cg_ok=premium_cg_ok,
        blockers=blockers,
    )


# ---------------------------------------------------------------------------
# FfmpegFeatureReport (transitional / utility-path only)
# ---------------------------------------------------------------------------

_FFMPEG_UNAVAILABLE_HINT = "Install ffmpeg to use streaming and utility features."


def probe_ffmpeg_features(
    *,
    sdi_relay_settings: _SdiRelaySettings | None = None,
    runner: ProcRunner | None = None,
) -> FfmpegFeatureReport:
    """Ffmpeg version + muxer/filter/encoder feature flags.

    Transitional/utility-path detail only (S1 §3) — the box's playout
    engine is GStreamer (:func:`probe_engine_readiness`), not per-segment
    ffmpeg. ``has_decklink``/``byo_sdi_binary`` mirror ``SdiReadiness`` /
    ``SdiRelaySettings`` rather than re-probing.
    """

    settings = sdi_relay_settings or _SdiRelaySettings.from_env()
    run = runner or (lambda args: run_ffmpeg(args))

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return FfmpegFeatureReport(
            detected=False,
            supported=False,
            has_decklink=False,
            has_ndi=False,
            has_libx264=False,
            has_loudnorm=False,
            byo_sdi_binary=settings.ffmpeg_path,
            next_step=_FFMPEG_UNAVAILABLE_HINT,
        )

    try:
        version_result = run(["-hide_banner", "-version"])
        muxers_result = run(["-hide_banner", "-muxers"])
        filters_result = run(["-hide_banner", "-filters"])
        encoders_result = run(["-hide_banner", "-encoders"])
    except Exception:
        return FfmpegFeatureReport(
            detected=True,
            supported=False,
            has_decklink=False,
            has_ndi=False,
            has_libx264=False,
            has_loudnorm=False,
            byo_sdi_binary=settings.ffmpeg_path,
            next_step="ffmpeg detected but a feature probe failed; repair or reinstall it.",
        )

    from civiccast.stream._ffmpeg import (
        _H264_ENCODER_PRIORITY,
        _parse_ffmpeg_encoders,
        _parse_ffmpeg_version,
        _version_is_supported,
    )

    version = _parse_ffmpeg_version(version_result.stdout + "\n" + version_result.stderr)
    supported = _version_is_supported(version) if version else False

    muxers = f"{muxers_result.stdout}\n{muxers_result.stderr}"
    filters = f"{filters_result.stdout}\n{filters_result.stderr}"
    encoders = f"{encoders_result.stdout}\n{encoders_result.stderr}"

    # has_libx264 is derived from the resolver's own GPL-encoder policy
    # order (civiccast/stream/_ffmpeg.py: "hardware first, then Media
    # Foundation, then the royalty-free software encoder, then libx264
    # strictly last") rather than a fresh string literal here -- the GPL
    # encoder name is a policy fact the resolver owns
    # (tests/policy/test_ffmpeg_h264_encoder.py enforces that no *other*
    # production module names it), and reusing its real encoder parser
    # keeps this detection in sync with how the resolver itself decides.
    parsed_encoders = _parse_ffmpeg_encoders(encoders)
    has_gpl_h264_encoder = _H264_ENCODER_PRIORITY[-1] in parsed_encoders

    return FfmpegFeatureReport(
        detected=True,
        version=version,
        supported=supported,
        has_decklink="decklink" in muxers.lower(),
        has_ndi="libndi_newtek" in muxers.lower() or "ndi" in muxers.lower(),
        has_libx264=has_gpl_h264_encoder,
        has_loudnorm="loudnorm" in filters.lower(),
        byo_sdi_binary=settings.ffmpeg_path,
        next_step="" if supported else "Upgrade ffmpeg to at least 4.4.",
    )


# ---------------------------------------------------------------------------
# ClockReport
# ---------------------------------------------------------------------------

NtpQuery = Callable[[], bool | None]


def _default_ntp_query() -> bool | None:
    """Best-effort NTP-sync query. Returns None (never a guess) on any doubt."""

    if os.name == "nt":
        w32tm = shutil.which("w32tm")
        if not w32tm:
            return None
        try:
            completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
                [w32tm, "/query", "/status"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        if completed.returncode != 0:
            return None
        output = completed.stdout.lower()
        if "last successful sync time" in output:
            return True
        return None
    timedatectl = shutil.which("timedatectl")
    if not timedatectl:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
            [timedatectl, "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip().lower()
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def probe_clock(*, ntp_query: NtpQuery | None = None) -> ClockReport:
    """System clock sanity for a 24/7 program log (S1 §3).

    ``ntp_sync`` fails to ``"unknown"`` — never a faked ``"synced"`` —
    when the query tool is absent or errors.
    """

    now = datetime.now().astimezone()
    offset = now.utcoffset()
    offset_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
    query = ntp_query or _default_ntp_query
    try:
        result = query()
    except Exception:
        result = None
    if result is True:
        ntp_sync: NtpSyncState = "synced"
        note = ""
    elif result is False:
        ntp_sync = "unsynced"
        note = "System clock is not NTP-synced; program-log timestamps may drift."
    else:
        ntp_sync = "unknown"
        note = "NTP sync status could not be determined on this host."
    return ClockReport(
        timezone=str(now.tzinfo) if now.tzinfo else "UTC",
        utc_offset_minutes=offset_minutes,
        system_time=datetime.now(UTC),
        ntp_sync=ntp_sync,
        note=note,
    )


# ---------------------------------------------------------------------------
# §6.3 Cable-grade OS verdict
# ---------------------------------------------------------------------------


def compute_cable_os_verdict(os_kind: OSKind) -> CableOsVerdict:
    """MASTER §13.1 soak-pending verdict (S1 §6.3)."""

    if os_kind == "linux":
        return CableOsVerdict(
            verdict="native-linux-recommended",
            os_kind=os_kind,
            rationale="Native Linux is the spec.md §5.3 cable-grade recommendation.",
        )
    if os_kind == "macos":
        return CableOsVerdict(
            verdict="native-linux-recommended",
            os_kind=os_kind,
            rationale=(
                "Cable on Mac runs CivicCast on a Linux box attached to the "
                "DeckLink card, per spec.md §5.3/§10.4."
            ),
        )
    # windows or wsl2 or unknown: soak-pending until MASTER §13.1 resolves.
    # The product never prints a green single-Windows-PC cable certification
    # before that decision (S1 §6.3, §9 item 6).
    return CableOsVerdict(
        verdict="soak-pending",
        os_kind=os_kind,
        rationale=(
            "Single-Windows-PC certification for 24/7 cable is pending the "
            "soak result — see MASTER §13.1."
        ),
    )


# ---------------------------------------------------------------------------
# §6.2 PEG readiness roll-up
# ---------------------------------------------------------------------------


def compute_peg_readiness(
    *,
    deployment_profile: DeploymentProfile,
    engine: EngineReadiness,
    qualified_tier: EngineTierVerdict,
    sdi: SdiReadiness,
    tsduck: TsduckStatus,
    clock: ClockReport,
    backup_destination: BackupDestinationRef,
    ram_total_gb: float,
    cable_os_verdict: CableOsVerdict,
) -> PegReadinessRollup:
    """Fail-closed green/yellow/red roll-up (S1 §6.2).

    ``deployment_profile == "peg-cable"`` is the only profile that treats
    DeckLink/TSDuck/SDI-tier engine prerequisites as required; other
    profiles report those dimensions ``green`` with a "not required for
    this deployment profile" message (the roll-up's color vocabulary is
    the shared green/yellow/red — spec's "not-applicable" language is
    rendered as green-with-explanation, never a nag). The base GStreamer
    engine is required for every profile (no engine, no playout).

    Note: the spec's red rule also lists "no working channel output
    path" — that is a channel/egress-config concern (S3's commissioning
    layer has that context), not something this box-level capability
    probe can evaluate; it is intentionally not implemented here rather
    than faked.
    """

    cable_required = deployment_profile == "peg-cable"
    dimensions: list[PegReadinessDimension] = []

    # Engine (always required)
    if not engine.gstreamer_present or not qualified_tier.base_ok:
        dimensions.append(
            PegReadinessDimension(
                id="engine",
                label="Playout engine",
                color="red",
                message="GStreamer engine is absent or missing a base required plugin.",
                next_step=engine.next_step or _GST_INSPECT_HINT,
            )
        )
    else:
        dimensions.append(
            PegReadinessDimension(
                id="engine",
                label="Playout engine",
                color="green",
                message="GStreamer base engine is ready.",
            )
        )

    # Clock
    if clock.ntp_sync == "unknown":
        dimensions.append(
            PegReadinessDimension(
                id="clock",
                label="Clock",
                color="yellow",
                message="NTP sync status could not be determined.",
                next_step="Verify system time sync manually (w32tm /query /status or timedatectl).",
            )
        )
    elif clock.ntp_sync == "unsynced":
        dimensions.append(
            PegReadinessDimension(
                id="clock",
                label="Clock",
                color="yellow",
                message="System clock is not NTP-synced.",
                next_step="Enable NTP time sync on the station.",
            )
        )
    else:
        dimensions.append(
            PegReadinessDimension(
                id="clock", label="Clock", color="green", message="Clock is NTP-synced."
            )
        )

    # DeckLink / SDI engine tier
    if not cable_required:
        dimensions.append(
            PegReadinessDimension(
                id="decklink",
                label="DeckLink / SDI",
                color="green",
                message="Not required for this deployment profile.",
            )
        )
    elif not qualified_tier.sdi_broadcast_ok:
        blocker = next((b for b in qualified_tier.blockers if b.tier == "sdi-broadcast"), None)
        dimensions.append(
            PegReadinessDimension(
                id="decklink",
                label="DeckLink / SDI",
                color="yellow",
                message=blocker.reason if blocker else "SDI/broadcast prerequisites not met.",
                next_step=blocker.next_step if blocker else "",
            )
        )
    else:
        dimensions.append(
            PegReadinessDimension(
                id="decklink",
                label="DeckLink / SDI",
                color="green",
                message="SDI/broadcast engine prerequisites are ready.",
            )
        )

    # TSDuck
    if not cable_required:
        dimensions.append(
            PegReadinessDimension(
                id="tsduck",
                label="TSDuck",
                color="green",
                message="Not required for this deployment profile.",
            )
        )
    elif not tsduck.installed:
        dimensions.append(
            PegReadinessDimension(
                id="tsduck",
                label="TSDuck",
                color="yellow",
                message="TSDuck is not installed.",
                next_step=tsduck.install_hint,
            )
        )
    else:
        dimensions.append(
            PegReadinessDimension(
                id="tsduck",
                label="TSDuck",
                color="green",
                message=f"TSDuck {tsduck.version or ''} installed.".strip(),
            )
        )

    # Backup destination
    if not backup_destination.configured:
        dimensions.append(
            PegReadinessDimension(
                id="backup",
                label="Backup destination",
                color="yellow",
                message="No backup destination is configured.",
                next_step="Configure a backup destination in the installer.",
            )
        )
    elif backup_destination.reachable is False:
        dimensions.append(
            PegReadinessDimension(
                id="backup",
                label="Backup destination",
                color="yellow",
                message="Configured backup destination is not reachable.",
                next_step="Check the backup destination's connectivity/credentials.",
            )
        )
    else:
        dimensions.append(
            PegReadinessDimension(
                id="backup",
                label="Backup destination",
                color="green",
                message="Backup destination is configured and reachable.",
            )
        )

    # AI-default RAM headroom
    if ram_total_gb < 8:
        dimensions.append(
            PegReadinessDimension(
                id="ai_ram",
                label="AI model memory",
                color="yellow",
                message=f"{ram_total_gb}GB system RAM is under the recommended floor.",
                next_step="Add system RAM; 16GB+ unlocks the 12B summary default.",
            )
        )
    else:
        dimensions.append(
            PegReadinessDimension(
                id="ai_ram",
                label="AI model memory",
                color="green",
                message=f"{ram_total_gb}GB system RAM detected.",
            )
        )

    # Cable-grade OS verdict
    if not cable_required:
        dimensions.append(
            PegReadinessDimension(
                id="cable_os",
                label="Cable-grade OS",
                color="green",
                message="Not required for this deployment profile.",
            )
        )
    elif cable_os_verdict.verdict == "soak-pending":
        dimensions.append(
            PegReadinessDimension(
                id="cable_os",
                label="Cable-grade OS",
                color="yellow",
                message=cable_os_verdict.rationale,
                next_step="Awaiting MASTER §13.1 soak decision.",
            )
        )
    else:
        dimensions.append(
            PegReadinessDimension(
                id="cable_os",
                label="Cable-grade OS",
                color="green",
                message=cable_os_verdict.rationale,
            )
        )

    colors = {dimension.color for dimension in dimensions}
    overall: PegReadinessColor
    if "red" in colors:
        overall = "red"
    elif "yellow" in colors:
        overall = "yellow"
    else:
        overall = "green"

    return PegReadinessRollup(overall=overall, dimensions=dimensions)


# ---------------------------------------------------------------------------
# Full profile assembly
# ---------------------------------------------------------------------------


def probe_station_box_profile(
    *,
    deployment_profile: DeploymentProfile = "public-meetings",
    backup_status: BackupStatus | None = None,
    hardware_probe: HardwareProbe | None = None,
    engine_runner: ProcRunner | None = None,
    device_runner: ProcRunner | None = None,
    ffmpeg_runner: ProcRunner | None = None,
    ntp_query: NtpQuery | None = None,
    tsduck_version_runner: Callable[[str], str] | None = None,
) -> StationBoxProfile:
    """Build the full StationBoxProfile from live probes (S1 §3-6).

    All injectable-runner parameters exist purely for deterministic
    testing; production callers omit them and get the real subprocess
    probes, mirroring the existing ``cached_check_sdi_runtime`` /
    ``locate_tsduck`` injection pattern.
    """

    from civiccast._version import __version__

    hw = hardware_probe or probe()
    sdi_settings = _SdiRelaySettings.from_env()
    sdi = (
        cached_check_sdi_runtime(sdi_settings.ffmpeg_path, ffmpeg_runner=None)
        if sdi_settings.ffmpeg_path
        else SdiReadiness(
            status="ffmpeg_unavailable",
            ffmpeg_detected=False,
            muxer_present=False,
            next_step=(
                "No BYO SDI ffmpeg is configured (CIVICCAST_SDI_FFMPEG); the "
                "GStreamer decklinkvideosink path is the canonical SDI output "
                "going forward (S15) — see engine.decklink."
            ),
        )
    )
    tsduck = locate_tsduck(version_runner=tsduck_version_runner)
    engine = probe_engine_readiness(
        native_os=hw.os.kind in ("windows", "linux", "macos"),
        gpu=hw.gpu,
        runner=engine_runner,
        device_runner=device_runner,
    )
    qualified_tier = compute_engine_tier_verdict(engine)
    ffmpeg_report = probe_ffmpeg_features(sdi_relay_settings=sdi_settings, runner=ffmpeg_runner)
    clock = probe_clock(ntp_query=ntp_query)
    network = NetworkReport(
        hostname=hw.os.hostname, primary_interface_up=True, headend_interface_hint=None
    )
    if backup_status is not None:
        backup_ref = BackupDestinationRef(
            configured=backup_status.status != "not_set_up",
            reachable=(backup_status.status == "ready"),
            destination=backup_status.destination,
            last_probe_at=backup_status.last_probe_at,
        )
    else:
        backup_ref = BackupDestinationRef(configured=False, reachable=None, destination=None)
    release_identity = ReleaseIdentityRef(
        version=hw.civiccast_version, package_verified=None, proof_state=None
    )
    ai_default = select_ai_defaults(hw)
    cable_os_verdict = compute_cable_os_verdict(hw.os.kind)
    peg_readiness = compute_peg_readiness(
        deployment_profile=deployment_profile,
        engine=engine,
        qualified_tier=qualified_tier,
        sdi=sdi,
        tsduck=tsduck,
        clock=clock,
        backup_destination=backup_ref,
        ram_total_gb=hw.ram.total_gb,
        cable_os_verdict=cable_os_verdict,
    )

    return StationBoxProfile(
        generated_at=datetime.now(UTC),
        civiccast_version=__version__,
        hardware=hw,
        system_ram_total_gb=hw.ram.total_gb,
        engine=engine,
        ffmpeg=ffmpeg_report,
        clock=clock,
        network=network,
        backup_destination=backup_ref,
        release_identity=release_identity,
        sdi=sdi,
        tsduck=tsduck,
        ndi_sdk=engine.ndi_sdk,
        qualified_engine_tier=qualified_tier,
        ai_default=ai_default,
        peg_readiness=peg_readiness,
        cable_os_verdict=cable_os_verdict,
    )
