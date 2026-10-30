# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Journaled PostgreSQL provisioning engine for CivicCast (Native).

Implements the WP2 provisioning slice named in
``.agent-runs/native-windows/specs/spec-installer-lifecycle.md`` (D4's
inventory: LocalSystem service, ``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl``,
firewall rules -- this module produces the DatabaseUrl VALUE and the
database server provisioning; the HKLM write, service SCM registration, and
firewall rules are the NSIS/service installer slice) and
``.agent-runs/native-windows/specs/spec-native-beta-recovery.md`` WP2 ("real
PostgreSQL, ... TSDuck, service, ACL, firewall, and registry provisioning"
-- this module covers the PostgreSQL portion; TSDuck, ACL, firewall, and the
registry write are out of this module's scope, see the evidence file for the
explicit scope boundary).

NATS JetStream was removed from the product entirely (owner decision
2026-08-20, ADR 0023 "NATS removed -- in-process event bus", which
supersedes ADR 0001); this package no longer provisions a NATS JetStream
store directory or ``nats-server`` config file.

Given a laid runtime tree and a verified server-binaries pack, this package:

1. Initializes (or safely detects and reuses) a PostgreSQL 17 data directory,
   writes the product's minimal ``postgresql.conf``/``pg_hba.conf`` deltas,
   and produces the ``DatabaseUrl`` value the installer writes to the
   registry (:func:`civiccast.native.provision.models.resolve_database_url`).
2. Journals every operation (backup-verify-before-mutate where mutating,
   fail-loud on any unexpected state, never silently repairs) so a killed
   run resumes cleanly.
3. Verifies the server-binaries pack's signature and byte inventory BEFORE
   running anything from it (:mod:`civiccast.native.provision.pack`, which
   delegates to :mod:`civiccast.installer.native_packs`).

Design boundaries (honest, do not overclaim):

* **Orchestration is pure; the real Windows/Postgres actions are SEAMS.**
  The engine (:mod:`civiccast.native.provision.orchestrator`) drives an
  injected :class:`~civiccast.native.provision.models.ProvisionSeams`
  bundle, mirroring :mod:`civiccast.native.upgrade`'s design exactly. The
  default bundle (:mod:`civiccast.native.provision.seams`) wires to real
  ``initdb``/filesystem actions; unit tests substitute fakes, so the fakes
  exercise the REAL orchestration + journal state machine, not a
  re-implementation of it. No unit test in this package spawns a real
  PostgreSQL process -- that proof belongs to the WP2/WP5 live lifecycle
  matrix.
* **What is explicitly deferred** is enumerated in
  ``.agent-runs/native-windows/ws5-installer/evidence/wp2-provisioning-postgres-nats-2026-07-29.md``:
  the Windows service wrapper's actual SCM registration, firewall rules, and
  the HKLM registry write.
"""

from __future__ import annotations

from civiccast.native.provision.conf import (
    render_pg_hba_conf,
    render_postgresql_conf,
)
from civiccast.native.provision.journal import (
    JournalError,
    load_journal,
    write_journal,
)
from civiccast.native.provision.models import (
    PostgresClusterDecision,
    ProvisionContext,
    ProvisionJournal,
    ProvisionOutcome,
    ProvisionPhase,
    ProvisionPlan,
    ProvisionRecovery,
    ProvisionSeams,
    build_database_url,
    evaluate_postgres_cluster,
    resolve_database_url,
)
from civiccast.native.provision.orchestrator import run_provision
from civiccast.native.provision.pack import (
    SERVER_BINARIES_COMPONENT,
    verify_server_binaries_pack,
)

__all__ = [
    "SERVER_BINARIES_COMPONENT",
    "JournalError",
    "PostgresClusterDecision",
    "ProvisionContext",
    "ProvisionJournal",
    "ProvisionOutcome",
    "ProvisionPhase",
    "ProvisionPlan",
    "ProvisionRecovery",
    "ProvisionSeams",
    "build_database_url",
    "evaluate_postgres_cluster",
    "load_journal",
    "render_pg_hba_conf",
    "render_postgresql_conf",
    "resolve_database_url",
    "run_provision",
    "verify_server_binaries_pack",
    "write_journal",
]
