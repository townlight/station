# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""TOCTOU-safe process reaping (S9 §6.3) — the shared kill primitive used by the
encoder-orphan terminator (daemon) and the optional device-holding co-processes
(CasparCG/SDI → DeckLink card, NDI runtime → NDI name).

PID reuse is a race: a probe says "pid 1234 is ours," but by kill time 1234 may be
sshd. The guard is the process create time — re-verified at kill time, never killed if
it drifted beyond ``tolerance_seconds`` (a recycled pid).
"""

from __future__ import annotations

import logging

_LOG = logging.getLogger(__name__)


def verify_and_kill_process(
    pid: int,
    created_at: float,
    *,
    tolerance_seconds: float = 1.0,
    terminate_timeout: float = 10.0,
) -> bool:
    """Terminate ``pid`` only if its current ``create_time`` matches ``created_at``
    within ``tolerance_seconds``. Returns True iff the process was killed.

    Returns False (does not kill) when: the pid is gone, the create time differs (a
    recycled pid — not ours), or access is denied (logged — a device-holding co-process
    may then remain locked). Never raises.
    """
    import psutil

    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created_at) > tolerance_seconds:
            return False  # pid was recycled since the probe — leave it alone
        process.terminate()
        process.wait(timeout=terminate_timeout)
        return True
    except psutil.NoSuchProcess:
        return False
    except psutil.TimeoutExpired:
        process.kill()  # graceful terminate timed out — force it
        return True
    except psutil.AccessDenied:
        _LOG.warning(
            "Access denied reaping pid %s; if it is a device-holding co-process "
            "(CasparCG/SDI or NDI runtime) the device may remain locked.",
            pid,
        )
        return False
