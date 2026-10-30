# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Journaled install/upgrade engine for CivicCast (Native) -- spec D3.

This package implements the beta-item-4 installer slice's WP-3: the
journaled, idempotently-resumable upgrade engine that the native NSIS hook
set (``nsis-hooks-native.nsh``) invokes. It orchestrates the spec D3 sequence

  1. journal phase-0 + acquire the shared D7a maintenance interlock
  2. stop/drain writers + verify quiescence (WS2 snapshot equality)
  3. VERIFIED pre-upgrade DB backup (WS2 machinery) BEFORE any mutation
  4. lay ``app\\<new>\\`` and flip the ``current`` junction
  5. ``alembic upgrade head``
  6. start service in maintenance/read-only health mode
  7. health green => release interlock (commit); journal complete

with the exact D3 failure semantics: failure at/after step 5 flips the
junction back AND restores the step-3 backup; a rollback-restore failure
HALTS with the service stopped, preserves the backup + journal, and emits an
operator recovery document; a release that declares its migration cannot
restore-roll-back is refused for auto-upgrade unless an operator ack is
supplied.

Design boundaries (honest, do not overclaim):

* **Orchestration is pure; the real Windows/Postgres actions are SEAMS.** The
  engine (:mod:`civiccast.native.upgrade.orchestrator`) drives an injected
  :class:`~civiccast.native.upgrade.models.UpgradeSeams` bundle. The default
  bundle (:mod:`civiccast.native.upgrade.seams`) wires to the real WS2 backup
  machinery (:mod:`civiccast.dr.backup`), the restore-drill spot check
  (:mod:`civiccast.dr.restore_drill`), the schema-currency reader
  (:mod:`civiccast.schema_check`), the D7a interlock probes
  (:mod:`civiccast.native.win_probes`), and the supervisor
  (:mod:`civiccast.native.supervisor`). Tests substitute fakes for the seams
  that need elevation or a live Postgres, so the fakes exercise the REAL
  orchestration + journal state machine, not a re-implementation of it. The
  junction flip is testable for real in a temp directory
  (:mod:`civiccast.native.upgrade.junction`).
* **What is proven by unit test now vs. deferred to the WP-4/5 live proof
  matrix** is enumerated explicitly in
  ``.agent-runs/native-windows/ws5-installer/evidence/wp3-unit-vs-live-matrix.md``.
* Every submodule imports on Linux (CI runs the pure suites there);
  Windows-only calls live behind the seam layer.
"""

from __future__ import annotations

from civiccast.native.upgrade.journal import (
    JournalError,
    load_journal,
    write_journal,
)
from civiccast.native.upgrade.models import (
    OperatorRecovery,
    UpgradeContext,
    UpgradeJournal,
    UpgradeOutcome,
    UpgradePhase,
    UpgradePlan,
    UpgradeSeams,
)
from civiccast.native.upgrade.orchestrator import run_upgrade

__all__ = [
    "JournalError",
    "OperatorRecovery",
    "UpgradeContext",
    "UpgradeJournal",
    "UpgradeOutcome",
    "UpgradePhase",
    "UpgradePlan",
    "UpgradeSeams",
    "load_journal",
    "run_upgrade",
    "write_journal",
]
