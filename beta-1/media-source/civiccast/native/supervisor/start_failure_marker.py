# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Operator-visible diagnosis for a station that cannot start.

Field evidence (candidate 4eca729, 2026-08-29): ``CivicCastSupervisor``
crash-looped every ~33s ("terminated unexpectedly. It has done this 371
time(s)" in the Windows Event Log). The Application log's own service entry
carried the real reason (``NativeStationConfigurationError``, an activation
self-test receipt mismatch) -- but ``supervisor.log`` held only the
unconditional "supervisor logging initialized" canary line
(``service_host.SvcDoRun``) and nothing else, because the exception that
crashed the host was never caught anywhere between the raising provider
(``service.default_dependency_provider``) and the SCM. An operator watching
ONLY this product's own logs -- the honest, documented place to look -- had
no way to learn why the station would not come up, and no signal that
waiting for "one more restart" would never help.

This module does not change WHETHER a corrupt/misconfigured station is
allowed to start: ``default_dependency_provider``'s existing distinction
between "not yet activated" (degrades gracefully, keeps running) and every
other :class:`~civiccast.native.station_runtime.NativeStationConfigurationError`
(fails loud, exits) is untouched and must stay untouched -- see that
function's own docstring for why. It only makes an already-loud failure
DIAGNOSABLE:

* :func:`record_start_failure` logs the exception's real type, message, and
  (via :func:`exception_diagnostic_detail`) any structured mismatch detail it
  carries, BEFORE the caller re-raises and the host exits -- so
  ``supervisor.log`` always carries the reason, not just the canary line.
* Once the SAME condition has recurred :data:`CONSECUTIVE_FAILURE_THRESHOLD`
  times in a row (a small persistent counter under the ProgramData root,
  alongside ``install-progress.log``), it writes :data:`MARKER_DOC_NAME`,
  an operator-readable markdown document -- matching the tone and placement
  of the provisioning engine's own honest-failure convention
  (``civiccast.native.provision.orchestrator.write_recovery_document``,
  ``PROVISION-RECOVERY.md``) -- stating plainly that the station cannot
  start, why, and that restarting will not fix it.
* :func:`record_start_success` clears both the counter and the marker the
  moment a start actually succeeds: a resolved problem must never leave a
  stale "cannot start" document behind for the next operator to trip over.

House lazy-import rule (matches ``civiccast.native.supervisor.service_env``):
this module imports only stdlib at module load, so the pure decision logic
(``tests/native/test_start_failure_marker.py``) runs on any platform; nothing
here touches ``winreg``, ``win32serviceutil``, or any other Windows-only API.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

#: The operator-facing marker document's name -- deliberately alongside
#: ``install-progress.log`` (both live directly under the ProgramData root),
#: so an operator who already knows to look there for install evidence finds
#: this too.
MARKER_DOC_NAME: Final[str] = "STATION-START-FAILED.md"

#: Internal bookkeeping only -- never presented to an operator directly.
_FAILURE_STATE_NAME: Final[str] = "station-start-failures.json"

#: After this many CONSECUTIVE failed starts, the marker document is written.
#: Chosen to ride out one or two transient failures (a momentarily locked
#: file, a slow disk on first boot) without alarming an operator over noise,
#: while still catching a genuine crash loop within about two minutes (the
#: registered SCM failure-action restart delay is ~33s, so 3 failures is
#: under 2 minutes -- see ``native_service_registration.rs``'s ``sc failure``
#: configuration).
CONSECUTIVE_FAILURE_THRESHOLD: Final[int] = 3


def _failure_state_path(civiccast_data_root: Path) -> Path:
    return civiccast_data_root / _FAILURE_STATE_NAME


def marker_path(civiccast_data_root: Path) -> Path:
    """Where :func:`record_start_failure` writes the operator-readable
    marker document -- a public name so callers (and tests) can check for it
    without re-deriving the join."""

    return civiccast_data_root / MARKER_DOC_NAME


def exception_diagnostic_detail(error: BaseException) -> str:
    """The best available human-readable detail for ``error`` -- ``str()``
    when the exception carries a real message (every
    :class:`~civiccast.native.station_runtime.NativeStationConfigurationError`
    does: e.g. "Native station activation self-test receipt does not match
    this distribution", which already names the mismatch), falling back to
    ``repr()`` only for the pathological case of an exception with no message
    at all -- never a blank string, which would defeat the entire point of
    logging this."""

    text = str(error)
    return text if text else repr(error)


def _read_failure_count(civiccast_data_root: Path) -> int:
    try:
        raw = json.loads(_failure_state_path(civiccast_data_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    count = raw.get("consecutive_failures") if isinstance(raw, dict) else None
    return count if isinstance(count, int) and count >= 0 else 0


def _marker_document_text(count: int, error_type: str, detail: str) -> str:
    lines = [
        "# CivicCast (Native) -- Station Cannot Start",
        "",
        f"- Written: {datetime.now(UTC).isoformat()}",
        f"- Consecutive failed starts: {count}",
        f"- Error type: {error_type}",
        f"- Reason: {detail}",
        "",
        "## The station has been crash-looping. Restarting it again will not fix this.",
        "",
        f"1. This is not a one-off: the station has failed to start {count} times in a "
        "row with the SAME error. Waiting for one more automatic restart will not help.",
        "2. The reason above names the exact condition blocking startup -- read it before "
        "doing anything else.",
        "3. Check supervisor.log (in the logs\\ folder alongside this file) for the full "
        "detail of this error, including which component or receipt disagreed if the "
        "reason mentions an activation self-test receipt or a caption tier.",
        "4. Do not delete or hand-edit station-set.json or activation-self-test.json "
        "hoping to clear this -- an incorrectly reconstructed one will fail the exact "
        "same fail-closed check for a different, harder-to-diagnose reason.",
        "5. Once the blocking condition is resolved and the station starts cleanly, this "
        "file and its underlying failure counter are removed automatically -- its "
        "continued presence means the station has not yet recovered.",
        "",
    ]
    return "\n".join(lines)


def record_start_failure(
    civiccast_data_root: Path, logger: logging.Logger, error: BaseException
) -> None:
    """Log ``error``'s real type and detail to ``logger`` (so it lands in
    supervisor.log, unlike before this existed), and once it has recurred
    :data:`CONSECUTIVE_FAILURE_THRESHOLD` times in a row, write the
    operator-readable marker document.

    Never raises: a failure to record a failure must not mask or replace the
    ORIGINAL failure, which the caller logs this for and then re-raises
    immediately afterward -- the existing fail-loud exit behavior for a
    corrupt/misconfigured station is completely unchanged by this function.
    """

    detail = exception_diagnostic_detail(error)
    error_type = type(error).__name__
    # Logged FIRST and unconditionally, before any filesystem I/O: this ONE
    # line -- naming the real exception type and detail -- is the entire
    # point of Defect B (field evidence: supervisor.log held only the
    # "logging initialized" canary, nothing about why the host crashed). It
    # must reach the log sink even if the counter/marker persistence below
    # fails for an unrelated reason (a locked file, a read-only mount).
    logger.error("station could not start: %s: %s", error_type, detail)
    try:
        civiccast_data_root.mkdir(parents=True, exist_ok=True)
        count = _read_failure_count(civiccast_data_root) + 1
        logger.info("consecutive failed start count is now %d", count)
        _failure_state_path(civiccast_data_root).write_text(
            json.dumps(
                {
                    "consecutive_failures": count,
                    "last_error_type": error_type,
                    "last_error_detail": detail,
                    "last_attempt_utc": datetime.now(UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        if count >= CONSECUTIVE_FAILURE_THRESHOLD:
            marker_path(civiccast_data_root).write_text(
                _marker_document_text(count, error_type, detail), encoding="utf-8"
            )
    except OSError:
        # Best-effort: the ORIGINAL exception (already logged above, or about
        # to be re-raised by the caller regardless) is what matters. A
        # failure to persist the counter or marker must never become a NEW,
        # more confusing exception in its own right.
        logger.warning("could not persist the station-start failure counter/marker", exc_info=True)


def record_start_success(civiccast_data_root: Path) -> None:
    """Clears any failure counter and marker document left by a prior crash
    loop. Called once a start actually succeeds -- a station that is running
    again is no longer crash-looping, and an operator must never be shown a
    stale "cannot start" document for a problem that is already gone."""

    for path in (_failure_state_path(civiccast_data_root), marker_path(civiccast_data_root)):
        with contextlib.suppress(OSError):
            path.unlink()


__all__ = [
    "CONSECUTIVE_FAILURE_THRESHOLD",
    "MARKER_DOC_NAME",
    "exception_diagnostic_detail",
    "marker_path",
    "record_start_failure",
    "record_start_success",
]
