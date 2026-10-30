# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Crash-recovery drill: kill a REAL child process, prove the real daemon restarts it.

Scope (see the package docstring for the full honesty statement): this drill
exercises :class:`civiccast.egress.daemon.EgressDaemon` — the actual
production crash-detection/relaunch code path (``process_once`` ->
``_poll_process`` -> ``_relaunch_after_crash``) — against a REAL OS child
process (a short-lived Python subprocess standing in for the ffmpeg child;
the daemon only needs something with a pid, a ``.poll()``, and a
``.terminate()``, which is exactly what ``ffmpeg_starter`` is the injection
seam for in production). The process is killed for real (``SIGKILL`` /
``TerminateProcess``, not a fake ``returncode`` assignment) and the drill
proves the daemon's own logic detects the real exit and relaunches a new
real process.

NOT covered by this rung: "a recording finalization interrupted mid-settle
recovers on next scan" (the roadmap's second crash-recovery scenario). See
the ``civiccast.dr`` package docstring for why, and what building it would
take.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from civiccast.dr.models import CrashDrillResult
from civiccast.egress.daemon import EgressDaemon
from civiccast.egress.models import (
    EgressCommand,
    EgressConfig,
    EgressSinkSpec,
    EgressSourcePlan,
    EgressSourceSegment,
)
from civiccast.egress.store import InMemoryEgressStore
from civiccast.stream._ffmpeg import FfmpegProcessHandle

_SLEEP_SCRIPT = "import time; time.sleep(60)"


def _spawn_real_child() -> FfmpegProcessHandle:
    """A real, short-lived OS process standing in for the ffmpeg child.

    Wrapped in the same :class:`FfmpegProcessHandle` production ``ffmpeg_starter``
    implementations return (see ``civiccast.egress.gst.strategy``) so the
    daemon is driven through its real, typed injection seam — not a raw
    ``Popen`` that happens to duck-type.
    """

    process = subprocess.Popen(  # noqa: S603 -- fixed args, no shell, no user input
        [sys.executable, "-c", _SLEEP_SCRIPT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return FfmpegProcessHandle(process=process)


def run_daemon_crash_restart_drill(
    *, work_dir: Path, channel_id: str = "dr-drill"
) -> CrashDrillResult:
    """Start a real child process under the real daemon, kill it, verify relaunch."""

    start = time.monotonic()
    work_dir.mkdir(parents=True, exist_ok=True)
    source_file = work_dir / "source.ts"
    source_file.write_text("dr-drill-source", encoding="utf-8")

    store = InMemoryEgressStore()
    store.upsert_config(
        EgressConfig(
            channel_id=channel_id,
            enabled=True,
            slate_message="CivicCast disaster-recovery drill channel.",
            sinks=[EgressSinkSpec(kind="file", label="Proof", uri=str(work_dir / "out.ts"))],
        )
    )
    store.enqueue_command(
        EgressCommand(
            channel_id=channel_id,
            action="start",
            issued_at=datetime.now(UTC),
            issued_by="dr-drill",
            command_id="dr-drill-start",
        )
    )

    spawned: list[FfmpegProcessHandle] = []

    def starter(_args: list[str]) -> FfmpegProcessHandle:
        proc = _spawn_real_child()
        spawned.append(proc)
        return proc

    def source_plan(_channel_id: str) -> EgressSourcePlan:
        return EgressSourcePlan(
            channel_id=channel_id,
            segments=[
                EgressSourceSegment(
                    label="DR drill source",
                    path=str(source_file),
                    duration_seconds=60,
                    source_ref="dr-drill-source",
                )
            ],
        )

    daemon = EgressDaemon(
        store,
        work_dir=work_dir,
        source_plan_provider=source_plan,
        ffmpeg_starter=starter,
        restart_cooldown_seconds=0.0,  # the drill wants an immediate, deterministic relaunch
    )

    try:
        daemon.process_once(channel_id)  # start -> real process #1
        if len(spawned) != 1:
            return CrashDrillResult(
                name="daemon_crash_restart",
                ok=False,
                detail=f"expected the daemon to start 1 process, started {len(spawned)}",
                duration_seconds=time.monotonic() - start,
            )
        first_pid = spawned[0].pid
        state_before = store.read_state(channel_id)
        if state_before is None or state_before.state != "ON_AIR":
            return CrashDrillResult(
                name="daemon_crash_restart",
                ok=False,
                detail=f"channel did not reach ON_AIR after start; state={state_before}",
                duration_seconds=time.monotonic() - start,
            )

        # Kill the REAL process — a genuine crash, not a fake returncode.
        spawned[0].process.kill()
        spawned[0].process.wait(timeout=10)

        daemon.process_once(channel_id)  # poll -> detect real exit -> relaunch
        if len(spawned) != 2:
            return CrashDrillResult(
                name="daemon_crash_restart",
                ok=False,
                detail=f"expected a relaunch to spawn a 2nd process, saw {len(spawned)} total",
                duration_seconds=time.monotonic() - start,
            )
        second_pid = spawned[1].pid
        state_after = store.read_state(channel_id)
        ok = state_after is not None and state_after.state == "ON_AIR" and second_pid != first_pid
        return CrashDrillResult(
            name="daemon_crash_restart",
            ok=ok,
            detail=(
                f"pid {first_pid} killed; daemon relaunched pid {second_pid}; "
                f"state after relaunch = {state_after.state if state_after else None!r}"
            ),
            duration_seconds=time.monotonic() - start,
        )
    finally:
        for handle in spawned:
            if handle.poll() is None:
                handle.process.kill()
                # Already killed; a stuck exit is not this drill's verdict to lose.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    handle.process.wait(timeout=10)
