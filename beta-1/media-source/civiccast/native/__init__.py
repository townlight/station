# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""civiccast.native -- the dual-runtime exclusion guard (slice:ws4-dual-runtime-guard).

Charter Sec.6 / gate 4: on a machine with both a WSL CivicCast install and a
native one, at most one runtime may transmit. This package is the native
(Windows) half of that guarantee: it decides, from the authoritative
selector plus live activity probes, whether the native runtime may start or
must stand down, and it enforces that decision continuously (not just at
startup).

Honest boundaries (do not overclaim beyond these):

- This package decides and enforces WHICH runtime may transmit on the
  Windows side. It does NOT prove the WSL side is patched -- the mirror-image
  ``ExecCondition`` + mutex participation on the in-distro service is an
  owner-routed rc-line change (see
  ``.agent-runs/native-windows/ws4-dual-runtime-guard/wsl-keeper-patch/``),
  shipped here only as a ready-but-unapplied diff and evidence.
- This package does NOT supervise child processes -- that is ws5's
  supervisor, which consumes ``GuardMonitor`` as a library.
- Its Windows probes are only as good as the process table, registry, and
  ``wsl.exe`` surfaces they read. A probe that cannot read its surface
  fails closed (``blocked_probe_unavailable`` / "unreadable"); it never
  silently assumes safety.

Module import of this package (and every submodule) succeeds on Linux --
Windows-only imports (``win32event``, ``win32security``, ``win32api``,
``pywintypes``) are lazy, confined to ``os.name == "nt"``-guarded code paths
inside ``win_probes.py``. CI's ``test`` job (ubuntu) runs the pure decision
table and monitor suites there; ``windows-latest`` runs the full tree
including the real-probe tests.
"""

from __future__ import annotations

from civiccast.native.models import (
    A1Result,
    A2Result,
    A3Result,
    CutoverJournal,
    CutoverPhaseRecord,
    GuardAction,
    GuardDecision,
    GuardInputs,
    InterlockRead,
    InterlockStatus,
    MaintenanceRecord,
    MutexStatus,
    ProbeStatus,
    Selector,
    SelectorRead,
)
from civiccast.native.runtime_guard import GuardMonitor, GuardMonitorStatus, decide
from civiccast.native.win_probes import (
    RuntimeOwnerMutex,
    detect_wsl_install,
    probe_indistro_services,
    probe_keeper,
    read_interlock,
    read_selector,
    release_interlock,
    scan_run_entries,
    take_interlock,
    write_selector,
)

__all__ = [
    "A1Result",
    "A2Result",
    "A3Result",
    "CutoverJournal",
    "CutoverPhaseRecord",
    "GuardAction",
    "GuardDecision",
    "GuardInputs",
    "GuardMonitor",
    "GuardMonitorStatus",
    "InterlockRead",
    "InterlockStatus",
    "MaintenanceRecord",
    "MutexStatus",
    "ProbeStatus",
    "RuntimeOwnerMutex",
    "Selector",
    "SelectorRead",
    "decide",
    "detect_wsl_install",
    "probe_indistro_services",
    "probe_keeper",
    "read_interlock",
    "read_selector",
    "release_interlock",
    "scan_run_entries",
    "take_interlock",
    "write_selector",
]
