# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""BYO-TSDuck headend compliance probe (cable automation CA-7).

TSDuck (BSD-licensed, https://tsduck.io) is brought by the station, like
BYO-NDI: when ``tsp`` is installed the probe runs a bounded ``analyze``
capture of the live udp-ts output and evaluates it against the channel's
configured mux rate. Absent TSDuck the verdict is an honest ``not-run``
with an install pointer â€” never a faked pass.

Honesty boundary: the checks are an analyze-plugin subset aligned with
TR 101 290 priority-1 concerns (TS sync, continuity, PAT/PMT, PCR), plus
CBR mux-rate stability and the single-program requirement. This is not the
full TR 101 290 monitoring suite, and it is not headend field proof.

Listening posture (verified against tsp 3.44 on this machine): the ``ip``
input joins ``group:port`` for multicast destinations but binds the bare
port for unicast ones â€” so multicast streams can be probed alongside the
real headend, while unicast destinations are probed in place of the
receiver during commissioning. The capture uses a generous socket receive
buffer (``--buffer-size``) so a busy host does not drop datagrams and report
spurious continuity errors on a clean stream; see ``_DEFAULT_RCVBUF_BYTES``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from civiccast.egress.models import EgressConfig, EgressSinkSpec

VersionRunner = Callable[[str], str]
ProbeRunner = Callable[[list[str]], int]
PortConnector = Callable[[str, int, float], bool]

_INSTALL_HINT = (
    "TSDuck is not installed. Choose “Enable cable verification” in the "
    "operator console and CivicCast will download and install the free "
    "BSD-licensed toolkit (tsduck.io) for you — no separate setup, no admin "
    "rights. (Advanced: set CIVICCAST_TSDUCK_PATH to an existing tsp bin "
    "directory to bring your own.)"
)

_TSDUCK_HOME_ENV = "CIVICCAST_TSDUCK_HOME"


def managed_tsduck_dir() -> Path:
    """Where CivicCast keeps its pull-on-demand TSDuck install.

    A contained, per-user location (no admin, easily removed) so enabling cable
    verification never touches the system or needs a separate installer. Override
    with ``CIVICCAST_TSDUCK_HOME``; otherwise LOCALAPPDATA on Windows (the cable
    egress target) and the XDG data dir elsewhere."""

    configured = os.environ.get(_TSDUCK_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "CivicCast" / "tsduck"
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "civiccast" / "tsduck"


def _managed_tsp_candidate() -> str | None:
    """The CivicCast-managed ``tsp`` binary, if a pulled install is present."""

    managed = managed_tsduck_dir()
    if not managed.is_dir():
        return None
    # Deterministic (sorted) so discovery never depends on filesystem-walk order.
    matches = sorted(p for exe in ("tsp.exe", "tsp") for p in managed.rglob(exe) if p.is_file())
    return str(matches[0]) if matches else None


def _shipped_pack_tsp_candidate() -> str | None:
    """The ``tsp.exe`` shipped in the ``native-server-binaries`` pack, if this
    process is running from a real install (audit A1's single source of
    truth for the installed layout -- ``civiccast.native.supervisor.
    install_layout.resolve_install_layout``, the SAME helper the control
    plane already uses to find ``pg_ctl.exe``/``ffmpeg.exe`` beside their own
    packs/dependencies trees).

    Root cause fixed here: ``scripts/build_native_server_pack.py`` has always
    packed ``tsp.exe`` at ``payload/tsduck/bin/tsp.exe``, but nothing ever
    looked there -- ``locate_tsduck`` only checked ``CIVICCAST_TSDUCK_PATH``,
    the pull-on-demand managed directory, and PATH, so a fresh install logged
    "TSDuck (tsp) not found" even though tsp.exe was sitting on disk the
    whole time. On a dev interpreter (not the installed ``runtime\\
    pythonservice.exe``) ``resolve_install_layout`` still resolves structurally
    correct paths that simply do not exist -- ``is_file()`` below is the
    existence gate, same posture every other ``InstallLayout`` field uses."""

    try:
        from civiccast.native.supervisor.install_layout import resolve_install_layout

        candidate = resolve_install_layout().tsp_exe_path
    except Exception:
        # Pure path arithmetic today (never raises in practice), but a
        # lookup helper must never turn a broken environment into a crash --
        # "not found" is the honest, safe answer here, same as every other
        # candidate step in locate_tsduck.
        return None
    return str(candidate) if candidate.is_file() else None


_NOT_CLAIMED = [
    "Analyze-plugin subset aligned with TR 101 290 priority-1 concerns; "
    "not the full TR 101 290 monitoring suite.",
    "Machine verification only; not field-proven against a real cable "
    "headend until the first-station beta.",
]
_DEFAULT_TOLERANCE_PCT = 5.0

# UDP socket receive buffer (SO_RCVBUF) for the probe's live capture. The OS
# default is small (~64-200 KB); on a loaded host a bounded analyze capture
# overruns it and the kernel drops datagrams, which TSDuck reports as per-PID
# continuity discontinuities even though the *stream itself* is clean. Proven
# locally: an 8 KB buffer invents ~100 CC errors on a stream whose file capture
# shows 0, and >=64 KB is clean on the same bytes. This was the root cause of
# the 2026-06-13 24h-soak public-stream "continuity=fail" false-RED. A generous
# buffer makes the bounded capture effectively lossless, so any reported
# discontinuity is real -- which is why continuity stays zero-tolerance (a real
# cable headend will choke on genuine CC errors; we must not soften that).
# NOTE (Linux): SO_RCVBUF is clamped to net.core.rmem_max -- raise it there to
# use the full size. CivicCast's cable egress targets Windows, which has no clamp.
_DEFAULT_RCVBUF_BYTES = 16 * 1024 * 1024
_RCVBUF_ENV = "CIVICCAST_TSDUCK_RCVBUF_BYTES"


def _probe_rcvbuf_bytes() -> int:
    """Socket receive buffer for the probe; env-overridable for high-bitrate channels."""
    raw = os.environ.get(_RCVBUF_ENV)
    if raw and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_RCVBUF_BYTES


class TsduckStatus(BaseModel):
    """Whether the station has TSDuck available."""

    model_config = ConfigDict(extra="forbid")

    installed: bool
    path: str | None = None
    version: str | None = None
    install_hint: str = ""


class ComplianceCheck(BaseModel):
    """One named check from a probe run."""

    model_config = ConfigDict(extra="forbid")

    check: str
    status: Literal["pass", "fail"]
    detail: str


class ComplianceProbeResult(BaseModel):
    """Outcome of one bounded compliance probe."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = ""
    destination: str = ""
    probed_at: datetime | None = None
    seconds: Annotated[int, Field(ge=0)] = 0
    expected_muxrate_kbps: Annotated[int, Field(ge=0)] = 0
    tsduck_version: str | None = None
    checks: list[ComplianceCheck] = Field(default_factory=list)
    verdict: Literal["pass", "fail", "not-run"]
    detail: str = ""
    raw_report_path: str | None = None
    not_claimed: list[str] = Field(default_factory=lambda: list(_NOT_CLAIMED))


class HeadendReadinessRollup(BaseModel):
    """System Health input: verification posture across udp-ts channels."""

    model_config = ConfigDict(extra="forbid")

    udp_channels: Annotated[int, Field(ge=0)] = 0
    tsduck_installed: bool = False
    passes: list[str] = Field(default_factory=list)
    fails: list[str] = Field(default_factory=list)
    not_run: list[str] = Field(default_factory=list)


class DevicePortProbe(BaseModel):
    """TCP reachability for one port."""

    model_config = ConfigDict(extra="forbid")

    port: int
    reachable: bool


class DeviceProbeResult(BaseModel):
    """TCP reachability of a headend appliance's management surface."""

    model_config = ConfigDict(extra="forbid")

    host: str
    ports: list[DevicePortProbe]
    any_reachable: bool
    probed_at: datetime | None = None


def _default_version_runner(tsp_path: str) -> str:
    completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
        [tsp_path, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return (completed.stdout or "") + (completed.stderr or "")


def locate_tsduck(*, version_runner: VersionRunner | None = None) -> TsduckStatus:
    """Find ``tsp`` via CIVICCAST_TSDUCK_PATH (a bin dir or full path), a
    CivicCast-managed pull-on-demand install, the ``native-server-binaries``
    pack this product ships tsp.exe in, or PATH -- in that precedence order."""

    candidates: list[str] = []
    env_path = os.environ.get("CIVICCAST_TSDUCK_PATH")
    if env_path:
        env = Path(env_path)
        candidates.extend(
            str(option) for option in (env, env / "tsp.exe", env / "tsp") if option.is_file()
        )
    if not candidates:
        # A CivicCast-managed pull-on-demand install takes precedence over PATH so
        # enabling cable verification "just works" without any env wiring.
        managed = _managed_tsp_candidate()
        if managed:
            candidates.append(managed)
    if not candidates:
        # The tsp.exe this product ITSELF ships in the native-server-binaries
        # pack (payload/tsduck/bin/tsp.exe) -- a real install always has this
        # even when no separate pull-on-demand/BYO install was ever set up.
        shipped = _shipped_pack_tsp_candidate()
        if shipped:
            candidates.append(shipped)
    if not candidates:
        on_path = shutil.which("tsp")
        if on_path:
            candidates.append(on_path)
    if not candidates:
        return TsduckStatus(installed=False, install_hint=_INSTALL_HINT)
    tsp_path = candidates[0]
    # A tsp that cannot report its version cannot run the compliance probe, so
    # report it as NOT installed rather than a usable green — this is honest
    # (no false-ready on a broken/partial copy) and lets a re-install self-heal.
    try:
        raw = (version_runner or _default_version_runner)(tsp_path)
    except Exception:
        return TsduckStatus(installed=False, install_hint=_INSTALL_HINT)
    text = raw.strip()
    if not text:
        return TsduckStatus(installed=False, install_hint=_INSTALL_HINT)
    return TsduckStatus(installed=True, path=tsp_path, version=text.splitlines()[0].strip())


def build_compliance_probe_args(
    *,
    destination_uri: str,
    seconds: int,
    json_report_path: Path,
) -> list[str]:
    """tsp args for one bounded analyze capture of the destination."""

    parsed = urlsplit(destination_uri)
    host = parsed.hostname or ""
    port = parsed.port
    listen = f"{host}:{port}" if _is_multicast_host(host) else str(port)
    return [
        "-I",
        "ip",
        listen,
        # Generous socket receive buffer so the bounded live capture does not
        # drop datagrams under load and invent continuity errors (see
        # _DEFAULT_RCVBUF_BYTES). Without this the probe false-REDs on a clean
        # stream when the host is busy.
        "--buffer-size",
        str(_probe_rcvbuf_bytes()),
        "-P",
        "until",
        "--seconds",
        str(seconds),
        "-P",
        "analyze",
        "--json",
        "--output-file",
        str(json_report_path),
        "-O",
        "drop",
    ]


def evaluate_tsduck_report(
    report: dict[str, Any],
    *,
    expected_muxrate_kbps: int,
    tolerance_pct: float = _DEFAULT_TOLERANCE_PCT,
) -> ComplianceProbeResult:
    """Pure evaluation of a tsp ``analyze --json`` report.

    Field names are pinned to a real TSDuck 3.44 report captured on this
    machine (tests/egress/fixtures/tsduck-analyze-clean-cbr.json).
    """

    ts = report.get("ts", {})
    packets = ts.get("packets", {})
    ts_pids = ts.get("pids", {})
    services = ts.get("services", {})
    pid_rows = report.get("pids", [])

    checks: list[ComplianceCheck] = []

    measured_bps = int(ts.get("pcr-bitrate") or ts.get("bitrate") or 0)
    expected_bps = expected_muxrate_kbps * 1000
    if expected_bps > 0 and measured_bps > 0:
        drift_pct = abs(measured_bps - expected_bps) / expected_bps * 100.0
        ok = drift_pct <= tolerance_pct
        checks.append(
            ComplianceCheck(
                check="cbr-mux-rate",
                status="pass" if ok else "fail",
                detail=(
                    f"measured {measured_bps} b/s vs expected {expected_bps} b/s "
                    f"({drift_pct:.2f}% drift, tolerance {tolerance_pct:g}%)"
                ),
            )
        )
    else:
        checks.append(
            ComplianceCheck(
                check="cbr-mux-rate",
                status="fail",
                detail=(
                    f"no usable bitrate (measured {measured_bps} b/s, expected {expected_bps} b/s)"
                ),
            )
        )

    invalid_syncs = int(packets.get("invalid-syncs", 0))
    transport_errors = int(packets.get("transport-errors", 0))
    checks.append(
        ComplianceCheck(
            check="ts-sync",
            status="pass" if invalid_syncs == 0 and transport_errors == 0 else "fail",
            detail=(f"invalid-syncs={invalid_syncs}, transport-errors={transport_errors}"),
        )
    )

    discontinuities = sum(int(row.get("packets", {}).get("discontinuities", 0)) for row in pid_rows)
    checks.append(
        ComplianceCheck(
            check="continuity",
            status="pass" if discontinuities == 0 else "fail",
            detail=f"{discontinuities} discontinuities across {len(pid_rows)} pids",
        )
    )

    pat_seen = any(int(row.get("id", -1)) == 0 for row in pid_rows)
    pmt_seen = any(str(row.get("description", "")).upper().startswith("PMT") for row in pid_rows)
    checks.append(
        ComplianceCheck(
            check="pat-pmt",
            status="pass" if pat_seen and pmt_seen else "fail",
            detail=f"PAT {'seen' if pat_seen else 'missing'}, PMT {'seen' if pmt_seen else 'missing'}",
        )
    )

    pcr_pids = int(ts_pids.get("pcr", 0))
    checks.append(
        ComplianceCheck(
            check="pcr-present",
            status="pass" if pcr_pids > 0 else "fail",
            detail=f"{pcr_pids} pid(s) carry PCR",
        )
    )

    service_total = int(services.get("total", 0))
    checks.append(
        ComplianceCheck(
            check="single-program",
            status="pass" if service_total == 1 else "fail",
            detail=f"{service_total} service(s) in the stream (SPTS requires exactly 1)",
        )
    )

    verdict: Literal["pass", "fail"] = (
        "pass" if all(check.status == "pass" for check in checks) else "fail"
    )
    return ComplianceProbeResult(
        expected_muxrate_kbps=expected_muxrate_kbps,
        checks=checks,
        verdict=verdict,
    )


def _default_probe_runner(args: list[str]) -> int:
    completed = subprocess.run(  # noqa: S603 -- fixed args, no shell
        args,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return completed.returncode


def run_compliance_probe(
    config: EgressConfig,
    *,
    seconds: int,
    work_dir: Path,
    tsduck: TsduckStatus | None = None,
    runner: ProbeRunner | None = None,
    now: datetime | None = None,
) -> ComplianceProbeResult:
    """Run one bounded probe of the channel's udp-ts destination.

    The last result is persisted to ``work_dir/<channel>/compliance-last.json``
    so System Health can report it without re-running anything.
    """

    sink = _udp_ts_sink(config)
    started = now or datetime.now(UTC)
    status = tsduck or locate_tsduck()
    base = ComplianceProbeResult(
        channel_id=config.channel_id,
        destination=sink.uri,
        probed_at=started,
        seconds=seconds,
        expected_muxrate_kbps=_muxrate_kbps(sink),
        tsduck_version=status.version,
        verdict="not-run",
    )
    if not status.installed:
        result = base.model_copy(update={"detail": status.install_hint or _INSTALL_HINT})
        _persist_last(result, work_dir)
        return result

    channel_dir = work_dir / config.channel_id
    channel_dir.mkdir(parents=True, exist_ok=True)
    report_path = channel_dir / "compliance-report.json"
    args = [
        status.path or "tsp",
        *build_compliance_probe_args(
            destination_uri=sink.uri,
            seconds=seconds,
            json_report_path=report_path,
        ),
    ]
    exit_code = (runner or _default_probe_runner)(args)
    if exit_code != 0 or not report_path.exists():
        result = base.model_copy(
            update={
                "verdict": "fail",
                "detail": (
                    f"tsp exited {exit_code} without a report â€” no packets on "
                    f"{sink.uri}, a firewall, or a bad destination are the usual causes"
                ),
            }
        )
        _persist_last(result, work_dir)
        return result

    report = json.loads(report_path.read_text(encoding="utf-8"))
    evaluated = evaluate_tsduck_report(report, expected_muxrate_kbps=_muxrate_kbps(sink))
    result = base.model_copy(
        update={
            "checks": evaluated.checks,
            "verdict": evaluated.verdict,
            "raw_report_path": str(report_path),
        }
    )
    _persist_last(result, work_dir)
    return result


def read_last_probe(channel_id: str, work_dir: Path) -> ComplianceProbeResult | None:
    """Read the channel's last persisted probe result, if any."""

    last = work_dir / channel_id / "compliance-last.json"
    if not last.exists():
        return None
    try:
        return ComplianceProbeResult.model_validate_json(last.read_text(encoding="utf-8"))
    except Exception:
        return None


def _default_connector(host: str, port: int, timeout_s: float) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def probe_device(
    host: str,
    *,
    ports: list[int] | None = None,
    timeout_s: float = 3.0,
    connector: PortConnector | None = None,
    now: datetime | None = None,
) -> DeviceProbeResult:
    """TCP reachability of a headend appliance's management ports.

    Vendor-agnostic on purpose: TelVue HyperCasters and Leightronix
    UltraNEXUS boxes are both managed over their web UIs.
    """

    connect = connector or _default_connector
    checked = [
        DevicePortProbe(port=port, reachable=connect(host, port, timeout_s))
        for port in (ports or [80, 443])
    ]
    return DeviceProbeResult(
        host=host,
        ports=checked,
        any_reachable=any(item.reachable for item in checked),
        probed_at=now or datetime.now(UTC),
    )


def _udp_ts_sink(config: EgressConfig) -> EgressSinkSpec:
    for sink in config.sinks:
        if sink.kind == "udp-ts":
            return sink
    raise ValueError(
        f"channel {config.channel_id!r} has no udp-ts sink to verify â€” "
        "apply a headend delivery profile first"
    )


def _muxrate_kbps(sink: EgressSinkSpec) -> int:
    args = sink.extra_output_args
    if "-muxrate" in args:
        raw = args[args.index("-muxrate") + 1]
        digits = raw.lower().removesuffix("k")
        if digits.isdigit():
            return int(digits)
    return 0


def _persist_last(result: ComplianceProbeResult, work_dir: Path) -> None:
    channel_dir = work_dir / result.channel_id
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "compliance-last.json").write_text(result.model_dump_json(), encoding="utf-8")


def _is_multicast_host(host: str) -> bool:
    first_octet = host.split(".", 1)[0]
    return first_octet.isdigit() and 224 <= int(first_octet) <= 239
