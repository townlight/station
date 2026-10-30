# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8-5 self-test runner (spec §6.6).

A daily self-test runs the install-time readiness path + a short FileSink
continuity proof + a backup write/read/delete probe + a model-runtime ping.
The weekly run adds a restore rehearsal, an SRT continuity proof, a TSDuck
compliance probe, and an alert-channel test send. Each subcheck returns
pass/fail; the runner aggregates them into a ``SystemSelfTest`` (pass/warn/fail)
and raises a ``self-test-fail`` warning condition on a non-pass (resolving it on
the next clean run) so a persistent failure pages once.

This module owns the orchestration + persistence + alert lifecycle (fully
unit-tested with injected checks). The heavyweight probe wiring
(``build_system_health_report``, ``run_filesink_continuity_proof``,
``build_backup_status``, model ping, etc.) is assembled where the scheduler runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from civiccast.alerting.models import SelfTestKind, SystemSelfTest
from civiccast.alerting.store import (
    _find_firing_event,
    record_alert_condition,
    upsert_self_test,
)

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

    from sqlalchemy.orm import Session

    from civiccast.alerting.credentials import GetCredential
    from civiccast.egress.compliance import TsduckStatus
    from civiccast.stream._ffmpeg import FfmpegResult

    SessionFactory = Callable[[], AbstractContextManager[Session]]
    FfmpegRunner = Callable[[list[str]], FfmpegResult]


# Loopback SRT is a finite 4s smoke, so do not use the station-facing 2s latency.
_SRT_LOOPBACK_SELF_TEST_LATENCY_MS = 120


@dataclass
class SelfTestCheck:
    """One named subcheck. ``run`` returns True on pass.

    ``required`` checks failing make the whole run ``fail``; advisory checks
    failing (with all required passing) make it ``warn``.
    """

    name: str
    run: Callable[[], bool]
    required: bool = True


def _resource_ref(kind: SelfTestKind) -> str:
    return f"self-test:{kind}"


def run_self_test(
    session: Session,
    kind: SelfTestKind,
    checks: list[SelfTestCheck],
    *,
    now: datetime,
    started_at: datetime | None = None,
) -> SystemSelfTest:
    """Run *checks*, persist a SystemSelfTest, and fire/resolve the alert.

    A check that raises is treated as a failure (fail-closed) — a self-test
    must never report green because a probe blew up.
    """
    started = started_at or now
    results: dict[str, bool] = {}
    for check in checks:
        try:
            results[check.name] = bool(check.run())
        except Exception:  # a crashing probe is a failed check (fail-closed)
            results[check.name] = False

    failed_required = [c.name for c in checks if c.required and not results[c.name]]
    failed_advisory = [c.name for c in checks if not c.required and not results[c.name]]

    if failed_required:
        status = "fail"
        # F-RC3-5: this fires as a *warning*, and on a fresh, never-broadcast
        # station a required check like "readiness" legitimately isn't met yet.
        # "FAILED" read as a system crash to a new operator; "did not pass"
        # matches the warning severity and points them at setup, honestly.
        summary = f"{kind} self-test did not pass: {', '.join(failed_required)}"
    elif failed_advisory:
        status = "warn"
        summary = f"{kind} self-test passed with warnings: {', '.join(failed_advisory)}"
    else:
        status = "pass"
        summary = f"{kind} self-test passed ({len(checks)}/{len(checks)} checks)."

    test = SystemSelfTest(
        self_test_id=f"selftest-{uuid.uuid4().hex}",
        kind=kind,
        started_at=started,
        finished_at=now,
        status=status,  # type: ignore[arg-type]
        checks=results,
        summary=summary[:600],
    )
    upsert_self_test(session, test)

    ref = _resource_ref(kind)
    if status in ("warn", "fail"):
        record_alert_condition(
            session,
            kind="self-test-fail",
            resource_ref=ref,
            source_section="S8",
            summary=summary,
            observed_at=now,
        )
    elif _find_firing_event(session, "self-test-fail", ref) is not None:
        # Clean run — resolve the firing self-test-fail for this kind via the indexed
        # exact lookup (NOT a capped get_alert_events scan, which on a busy box can miss
        # a stale row outside its 200-row recency window and leave it firing forever).
        # Guarded on an actually-firing event so a clean run never writes a spurious
        # pre-resolved audit row (record_alert_condition(resolved=True) would otherwise
        # create one when nothing is firing).
        record_alert_condition(
            session,
            kind="self-test-fail",
            resource_ref=ref,
            source_section="S8",
            summary=f"{kind} self-test recovered",
            observed_at=now,
            resolved=True,
        )

    return test


# ---------------------------------------------------------------------------
# Subcheck assembly (which checks run for daily vs weekly, §6.6)
# ---------------------------------------------------------------------------


@dataclass
class SelfTestDeps:
    """Injected bool probes for each self-test subcheck (True = pass).

    The heavyweight implementations (readiness via ``build_system_health_report``,
    ``run_filesink_continuity_proof``, the backup write/read/delete probe, a model
    ping, restore rehearsal, SRT continuity, a TSDuck probe, an alert-channel test
    send) are wired by the daemon where the live engine + work dirs are available;
    here they are plain callables so assembly is testable and order-deterministic.
    """

    readiness: Callable[[], bool]
    filesink_continuity: Callable[[], bool]
    backup_probe: Callable[[], bool]
    model_ping: Callable[[], bool]
    restore_rehearsal: Callable[[], bool]
    srt_continuity: Callable[[], bool]
    tsduck_probe: Callable[[], bool]
    channel_test_send: Callable[[], bool]


def assemble_self_test_checks(kind: SelfTestKind, deps: SelfTestDeps) -> list[SelfTestCheck]:
    """Return the ordered subcheck set for *kind* (§6.6).

    Daily: install-time readiness + FileSink continuity proof + backup
    write/read/delete (all required) + a model-runtime ping (advisory — an
    optional AI runtime being down warns, it does not fail the box). Weekly adds
    a restore rehearsal + SRT continuity proof (required) and a TSDuck compliance
    probe + an alert-channel test send (advisory — BYO/optional surfaces).
    """
    daily = [
        SelfTestCheck("readiness", deps.readiness),
        SelfTestCheck("filesink_continuity", deps.filesink_continuity),
        SelfTestCheck("backup_probe", deps.backup_probe),
        SelfTestCheck("model_ping", deps.model_ping, required=False),
    ]
    if kind == "daily":
        return daily
    return [
        *daily,
        SelfTestCheck("restore_rehearsal", deps.restore_rehearsal),
        SelfTestCheck("srt_continuity", deps.srt_continuity),
        SelfTestCheck("tsduck_probe", deps.tsduck_probe, required=False),
        SelfTestCheck("channel_test_send", deps.channel_test_send, required=False),
    ]


def assemble_available_self_test_checks(
    kind: SelfTestKind,
    deps: SelfTestDeps,
    availability: dict[str, bool],
) -> list[SelfTestCheck]:
    """Like ``assemble_self_test_checks`` but EXCLUDES checks whose tooling is
    unavailable (``availability[name] is False``).

    This is the integrity guard: a proof that cannot run in this context (no
    ffmpeg, no TSDuck, no synthetic media) is honestly **omitted** — never
    recorded as a fabricated pass or a misleading fail. A name absent from the
    map defaults to included (the always-in-process light probes)."""
    return [
        check
        for check in assemble_self_test_checks(kind, deps)
        if availability.get(check.name, True)
    ]


# ---------------------------------------------------------------------------
# Default real probes (light, in-process) + precondition availability (§6.6)
# ---------------------------------------------------------------------------


def _readiness_ok() -> bool:
    """Install-time readiness path — green/yellow safe-to-broadcast = ready."""
    try:
        from civiccast.installer.service import build_system_health_report

        return build_system_health_report().safe_to_broadcast != "red"
    except Exception:
        return False


def _backup_ok() -> bool:
    """Backup write/read/delete proof — the durable backup volume is ready."""
    try:
        from civiccast.installer.service import build_backup_status

        return build_backup_status().status == "ready"
    except Exception:
        return False


def _model_ping() -> bool:
    """Best-effort reachability of the local model runtime (advisory check)."""
    import os

    import httpx

    base = os.environ.get("CIVICCAST_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        with httpx.Client(timeout=3.0) as client:
            return client.get(f"{base}/api/tags").status_code == 200
    except Exception:
        return False


def _restore_rehearsal_ok() -> bool:
    """Round-trip a restore-proof payload through the configured backup volume."""
    try:
        from civiccast.installer.service import run_restore_rehearsal

        return run_restore_rehearsal().status == "passed"
    except Exception:
        return False


def _not_wired(name: str) -> Callable[[], bool]:
    """A heavy proof not yet implemented (1b-ii). Gated off by availability so it
    is never invoked; if it ever were, it fails loudly rather than faking a pass."""

    def probe() -> bool:
        raise NotImplementedError(f"{name} self-test probe is wired in S8-5 step 1b-ii")

    return probe


def _ffmpeg_present() -> bool:
    import shutil

    return shutil.which("ffmpeg") is not None


def _generate_test_clip(work_dir: Path, ffmpeg_runner: FfmpegRunner) -> Path:
    """Generate a tiny self-contained MPEG-TS clip (no committed binary, no media
    on disk) using the bundled ffmpeg — 2s of colour bars + a 1 kHz tone."""
    work_dir.mkdir(parents=True, exist_ok=True)
    clip = work_dir / "selftest-clip.ts"
    ffmpeg_runner(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:sample_rate=48000:duration=2",
            # Normalize the tone to the broadcast loudness target so the proof
            # validates the pipeline, not an artificially quiet test tone.
            "-af",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v",
            "h264",
            "-c:a",
            "aac",
            "-t",
            "2",
            "-f",
            "mpegts",
            str(clip),
        ]
    )
    return clip


def filesink_continuity_probe(
    *,
    work_dir: Path | None = None,
    ffmpeg_runner: FfmpegRunner | None = None,
    clip_generator: Callable[[Path, FfmpegRunner], Path] | None = None,
) -> bool:
    """Real FileSink continuity proof for the daily self-test (§6.6).

    Generates a throwaway clip with the bundled ffmpeg, plays it through a real
    encode across a concat boundary, and returns whether the proof PASSED. The
    runner + clip generator are injectable so tests need neither ffmpeg nor media.
    """
    import tempfile
    from pathlib import Path as _Path

    from civiccast.egress.continuity import run_filesink_continuity_proof
    from civiccast.egress.models import (
        EgressConfig,
        EgressSinkSpec,
        EgressSourcePlan,
        EgressSourceSegment,
    )
    from civiccast.stream._ffmpeg import run_ffmpeg

    runner = ffmpeg_runner or run_ffmpeg
    generate = clip_generator or _generate_test_clip
    own_temp = work_dir is None
    wd = _Path(work_dir) if work_dir is not None else _Path(tempfile.mkdtemp(prefix="cc-selftest-"))
    try:
        clip = generate(wd, runner)
        # Two segments of the same clip exercise a real concat boundary.
        plan = EgressSourcePlan(
            channel_id="__selftest__",
            segments=[
                EgressSourceSegment(label="self-test A", path=str(clip), duration_seconds=2.0),
                EgressSourceSegment(label="self-test B", path=str(clip), duration_seconds=2.0),
            ],
        )
        config = EgressConfig(
            channel_id="__selftest__",
            enabled=True,
            slate_message="CivicCast self-test.",
            sinks=[EgressSinkSpec(kind="file", label="self-test", uri=str(wd / "selftest-out.ts"))],
        )
        proof = run_filesink_continuity_proof(
            source_plan=plan,
            config=config,
            output_path=wd / "proof.ts",
            work_dir=wd,
            ffmpeg_runner=runner,
        )
        return proof.status == "PASS"
    except Exception:
        return False
    finally:
        if own_temp:
            import shutil as _shutil

            _shutil.rmtree(wd, ignore_errors=True)


def _default_tsp_runner(args: list[str]) -> int:
    """Run a bounded tsp pipeline; return its exit code (0 = the stream parsed)."""
    import subprocess

    completed = subprocess.run(  # noqa: S603 - fixed tsp path + args, no shell
        args, capture_output=True, timeout=30, check=False
    )
    return completed.returncode


def tsduck_smoke_probe(
    *,
    work_dir: Path | None = None,
    ffmpeg_runner: FfmpegRunner | None = None,
    clip_generator: Callable[[Path, FfmpegRunner], Path] | None = None,
    tsp_runner: Callable[[list[str]], int] | None = None,
    locator: Callable[[], TsduckStatus] | None = None,
) -> bool:
    """Real TSDuck smoke for the weekly self-test (§6.6).

    Generates a throwaway MPEG-TS clip with the bundled ffmpeg and runs it through
    the real ``tsp ... -P analyze -O drop`` pipeline (the same analyze plugin the
    cable-compliance probe relies on); a clean exit means TSDuck is genuinely
    functional, not merely present. Returns False if TSDuck is not installed. All
    effects are injectable so tests need neither ffmpeg nor TSDuck."""
    import tempfile
    from pathlib import Path as _Path

    from civiccast.egress.compliance import locate_tsduck
    from civiccast.stream._ffmpeg import run_ffmpeg

    status = (locator or locate_tsduck)()
    if not status.installed or not status.path:
        return False
    runner = ffmpeg_runner or run_ffmpeg
    generate = clip_generator or _generate_test_clip
    run_tsp = tsp_runner or _default_tsp_runner
    own_temp = work_dir is None
    wd = (
        _Path(work_dir)
        if work_dir is not None
        else _Path(tempfile.mkdtemp(prefix="cc-tsduck-selftest-"))
    )
    try:
        clip = generate(wd, runner)
        rc = run_tsp([status.path, "-I", "file", str(clip), "-P", "analyze", "-O", "drop"])
        return rc == 0
    except Exception:
        return False
    finally:
        if own_temp:
            import shutil as _shutil

            _shutil.rmtree(wd, ignore_errors=True)


def _tsduck_available() -> bool:
    """TSDuck smoke can run only when both tsp (functional) and ffmpeg are present."""
    from civiccast.egress.compliance import locate_tsduck

    return locate_tsduck().installed and _ffmpeg_present()


def _ffmpeg_has_srt() -> bool:
    """Whether the bundled ffmpeg was built with the SRT protocol."""
    import shutil
    import subprocess

    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    try:
        completed = subprocess.run(  # noqa: S603 - fixed exe + args, no shell
            [exe, "-hide_banner", "-protocols"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    return "srt" in (completed.stdout + completed.stderr).lower()


def _srt_available() -> bool:
    """SRT continuity smoke needs ffmpeg built with SRT."""
    return _ffmpeg_present() and _ffmpeg_has_srt()


def _free_loopback_port() -> int:
    """A likely-free local UDP port for the loopback SRT proof.

    There is a benign bind/reuse race: between closing this probe socket and the
    SRT proof reusing the port number, another process could claim it. That fails
    CLOSED — run_srt_receiver_continuity_proof simply fails and the probe returns
    False (an advisory ``warn``), never a false-pass — so it is acceptable; the
    cost is at most an occasional spurious advisory warning, not a false result."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def srt_continuity_probe(
    *,
    work_dir: Path | None = None,
    ffmpeg_runner: FfmpegRunner | None = None,
    clip_generator: Callable[[Path, FfmpegRunner], Path] | None = None,
    port: int | None = None,
    proof_runner: Callable[..., object] | None = None,
) -> bool:
    """Real SRT receiver continuity proof for the weekly self-test (§6.6).

    Generates a throwaway clip, pushes it over a loopback SRT sender, receives it
    on a loopback SRT listener, and returns whether the proof PASSED (continuity
    held across a concat boundary). The heavy proof is injectable (``proof_runner``)
    so the orchestration unit-tests without ffmpeg/SRT; the real path runs when
    ffmpeg has SRT."""
    import tempfile
    from pathlib import Path as _Path

    from civiccast.egress.continuity import run_srt_receiver_continuity_proof
    from civiccast.egress.models import (
        EgressConfig,
        EgressSinkSpec,
        EgressSourcePlan,
        EgressSourceSegment,
    )
    from civiccast.stream._ffmpeg import run_ffmpeg

    runner = ffmpeg_runner or run_ffmpeg
    generate = clip_generator or _generate_test_clip
    run_proof = proof_runner or run_srt_receiver_continuity_proof
    loopback_port = port or _free_loopback_port()
    own_temp = work_dir is None
    wd = (
        _Path(work_dir)
        if work_dir is not None
        else _Path(tempfile.mkdtemp(prefix="cc-srt-selftest-"))
    )
    try:
        clip = generate(wd, runner)
        plan = EgressSourcePlan(
            channel_id="__selftest__",
            segments=[
                EgressSourceSegment(label="self-test A", path=str(clip), duration_seconds=2.0),
                EgressSourceSegment(label="self-test B", path=str(clip), duration_seconds=2.0),
            ],
        )
        config = EgressConfig(
            channel_id="__selftest__",
            enabled=True,
            slate_message="CivicCast self-test.",
            sinks=[
                EgressSinkSpec(
                    kind="srt",
                    label="self-test",
                    uri=f"srt://127.0.0.1:{loopback_port}",
                    latency_ms=_SRT_LOOPBACK_SELF_TEST_LATENCY_MS,
                )
            ],
        )
        proof = run_proof(
            source_plan=plan,
            config=config,
            sender_url=f"srt://127.0.0.1:{loopback_port}",
            receiver_url=f"srt://127.0.0.1:{loopback_port}?mode=listener",
            receiver_output_path=wd / "srt-recv.ts",
            work_dir=wd,
            ffmpeg_runner=runner,
        )
        return getattr(proof, "status", None) == "PASS"
    except Exception:
        return False
    finally:
        if own_temp:
            import shutil as _shutil

            _shutil.rmtree(wd, ignore_errors=True)


def channel_delivery_ready(
    session_factory: SessionFactory, credential_reader: GetCredential
) -> bool:
    """Delivery-READINESS check (NOT a live send — no weekly spam/cost): at least
    one enabled alert channel exists and is resolvable, so alerts can actually be
    delivered. A channel with a declared ``credential_handle`` is resolvable only
    if that secret is present (catches "channel added but its secret is gone, so
    alerts would silently never deliver"); a channel needing no secret resolves."""
    from civiccast.alerting.store import get_alert_channels

    try:
        with session_factory() as session:
            channels = get_alert_channels(session)
    except Exception:
        return False
    enabled = [c for c in channels if getattr(c, "enabled", True)]
    if not enabled:
        return False

    def _resolvable(channel: object) -> bool:
        handle = getattr(channel, "credential_handle", None)
        if not handle:
            return True  # no secret required to deliver
        try:
            return credential_reader(handle) is not None
        except Exception:
            return False

    return any(_resolvable(channel) for channel in enabled)


def _enabled_channel_exists(session_factory: SessionFactory) -> bool:
    from civiccast.alerting.store import get_alert_channels

    try:
        with session_factory() as session:
            return any(getattr(c, "enabled", True) for c in get_alert_channels(session))
    except Exception:
        return False


def default_self_test_deps(
    *,
    session_factory: SessionFactory | None = None,
    credential_reader: GetCredential | None = None,
) -> SelfTestDeps:
    """Real light probes (readiness, backup, model-ping) + the FileSink continuity
    proof + restore rehearsal + the TSDuck smoke + the SRT continuity proof. Only
    the alert-channel test send remains a placeholder here (it needs a session +
    credential store, so the app wires it); gated off by
    ``default_self_test_availability`` until then."""
    return SelfTestDeps(
        readiness=_readiness_ok,
        filesink_continuity=lambda: filesink_continuity_probe(),
        backup_probe=_backup_ok,
        model_ping=_model_ping,
        restore_rehearsal=_restore_rehearsal_ok,
        srt_continuity=lambda: srt_continuity_probe(),
        tsduck_probe=lambda: tsduck_smoke_probe(),
        channel_test_send=(
            (lambda: channel_delivery_ready(session_factory, credential_reader))
            if session_factory is not None and credential_reader is not None
            else _not_wired("channel_test_send")
        ),
    )


def default_self_test_availability(
    *, session_factory: SessionFactory | None = None
) -> dict[str, bool]:
    """Which subchecks can actually run now. The light in-process probes are
    always available; the heavy proofs flip on per real ffmpeg/TSDuck/SRT presence.
    The channel delivery-readiness check is available only when a session factory
    is wired AND at least one enabled alert channel exists (else honestly omitted)."""
    return {
        "readiness": True,
        "backup_probe": True,
        "model_ping": True,
        # FileSink continuity proof runs wherever the bundled encoder is present.
        "filesink_continuity": _ffmpeg_present(),
        # Restore rehearsal runs only when a backup destination is configured;
        # otherwise it is excluded (the backup_probe check already flags "no backup",
        # so we don't double-count the same gap).
        "restore_rehearsal": _backup_ok(),
        # TSDuck smoke runs only when tsp (functional) + ffmpeg are both present;
        # otherwise it is honestly omitted (never a false-RED for a station that
        # has not enabled cable verification).
        "tsduck_probe": _tsduck_available(),
        # SRT continuity runs only when ffmpeg was built with the SRT protocol;
        # otherwise honestly omitted (no false-RED on a station without SRT egress).
        "srt_continuity": _srt_available(),
        # The channel delivery-readiness check runs only when a session factory is
        # wired (the app provides it) AND an enabled channel exists; otherwise the
        # zero-arg default leaves it not-run (a station may run alerts UI-only).
        "channel_test_send": (
            _enabled_channel_exists(session_factory) if session_factory is not None else False
        ),
    }
