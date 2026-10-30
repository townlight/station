# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Out-of-process supervision for the emitted-caption proof loop.

The proof loop is observability work. It must never be able to hold the
control-plane process that owns live playout.  The supervisor in this module
keeps a small Python child alive; the child builds the normal production proof
worker against the station database and exits/restarts independently if it
crashes.  FFmpeg capture and decode are independently bounded in
``caption_proof``/``caption_proof_worker`` as a second line of defense.

The parent only owns the child lifecycle.  It does not share SQLAlchemy
connections, stores, or worker closures with the child, which keeps this safe
under Windows' spawn/subprocess model as well as on Unix.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from sqlalchemy.orm import Session

_LOG = logging.getLogger(__name__)


class CaptionProofProcessSupervisor:
    """Run the caption proof worker in a separately supervised process."""

    def __init__(
        self,
        *,
        poll_seconds: float,
        capture_seconds: float = 6.0,
        timeout_seconds: float = 15.0,
        work_dir: Path | None = None,
        python_executable: str | None = None,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if capture_seconds <= 0:
            raise ValueError("capture_seconds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._poll_seconds = poll_seconds
        self._capture_seconds = capture_seconds
        self._timeout_seconds = timeout_seconds
        self._work_dir = work_dir
        self._python_executable = python_executable or sys.executable
        self._popen = popen
        self._child: subprocess.Popen[str] | None = None

    @property
    def child_pid(self) -> int | None:
        child = self._child
        return child.pid if child is not None and child.poll() is None else None

    def _command(self) -> list[str]:
        return [
            self._python_executable,
            "-m",
            "civiccast.egress.caption_proof_process",
            "--worker-poll-seconds",
            str(self._poll_seconds),
            "--capture-seconds",
            str(self._capture_seconds),
            "--timeout-seconds",
            str(self._timeout_seconds),
            *(["--work-dir", str(self._work_dir)] if self._work_dir is not None else []),
        ]

    def _start_child(self) -> None:
        if self.child_pid is not None:
            return
        child = self._popen(
            self._command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            text=True,
            env={**os.environ, "CIVICCAST_CAPTION_PROOF_PROCESS_CHILD": "1"},
        )
        self._child = child
        _LOG.info("civiccast-caption-proof child started (pid=%s).", child.pid)

    def _stop_child(self) -> None:
        child = self._child
        self._child = None
        if child is None or child.poll() is not None:
            return
        with contextlib.suppress(OSError):
            child.terminate()
        try:
            child.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                child.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=5.0)

    def run_forever(self, *, poll_seconds: float, stop_event: Event) -> None:
        """Maintain the child until the app's ``ThreadSupervisor`` stops us."""

        restart_delay = min(5.0, max(0.5, poll_seconds))
        last_exit_at = 0.0
        try:
            while not stop_event.is_set():
                child = self._child
                if child is None or child.poll() is not None:
                    if child is not None:
                        code = child.poll()
                        _LOG.warning(
                            "civiccast-caption-proof child exited with code %s; "
                            "restarting after a bounded delay.",
                            code,
                        )
                        self._child = None
                        last_exit_at = time.monotonic()
                    if time.monotonic() - last_exit_at >= restart_delay:
                        self._start_child()
                stop_event.wait(min(1.0, max(0.1, poll_seconds)))
        finally:
            self._stop_child()


@contextmanager
def _session_factory() -> Iterator[Session]:
    """Yield a fresh DB session in the proof child.

    The child builds its own lazy engine from the inherited ``DATABASE_URL``;
    no parent-process SQLAlchemy state is reused.
    """

    from civiccast.db import get_session

    session = get_session()
    value = next(session)
    try:
        yield value
    finally:
        with contextlib.suppress(StopIteration):
            next(session)


def _run_worker(args: argparse.Namespace) -> None:
    from civiccast.egress.automation import default_egress_work_dir
    from civiccast.egress.caption_proof_worker import build_caption_proof_worker
    from civiccast.installer.station_state import resolve_live_captions_enabled_or_default

    work_dir = Path(args.work_dir) if args.work_dir else default_egress_work_dir()
    worker = build_caption_proof_worker(
        _session_factory,
        work_dir=work_dir,
        capture_seconds=args.capture_seconds,
        ffmpeg_timeout_seconds=args.timeout_seconds,
        is_enabled=resolve_live_captions_enabled_or_default,
    )
    worker.run_forever(poll_seconds=args.worker_poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CivicCast caption proof child")
    parser.add_argument("--worker-poll-seconds", type=float, default=30.0)
    parser.add_argument("--capture-seconds", type=float, default=6.0)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--work-dir", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    _run_worker(args)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the service child
    raise SystemExit(main())
