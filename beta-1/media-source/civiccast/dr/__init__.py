# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Disaster-recovery drills (0.5.0): backup/restore that is exercised, not assumed.

``civiccast.installer.service.run_restore_rehearsal`` (pre-existing) proves the
*backup destination* round-trips a proof token. That is a real, useful check of
storage plumbing, but it never touches the station's actual data — a corrupt
database or an unreadable media manifest would still show "passed". This
package is the drill underneath: it backs up the REAL database, restores it
into a completely fresh database, and verifies row counts, per-table content
checksums, and that the app's own stores can read the restored data.

The operator-facing portable backup workflow is separate from these drills
and lives in :class:`civiccast.dr.station_backup.StationBackupService`; it includes
encrypted configuration/credential payloads and complete selected media
files. The boundaries below describe the DR *drill* functions, not that
operator backup package.

Honest boundaries (do not overclaim beyond these):

* **The DR drill's media check is a manifest + bounded sampled hash, not
  replication.**
  Hashing every byte of a station's media library on every drill is not a
  drill, it is an outage. The manifest records path, size, and a sha256 of a
  bounded head/tail sample per file; operators still need their own
  file-level backup of media (rsync, robocopy, a NAS snapshot, ...). The
  drill verifies the manifest matches what is on disk today, not that every
  byte of every file is bit-identical to some prior backup.
* **Postgres backup/restore is implemented AND executed**
  (# claim:ws2-postgres-restore-drill), in CI, under a
  Docker gate. ``civiccast.dr.report.run_full_drill`` dispatches a
  ``postgresql://`` ``DATABASE_URL`` to a real ``pg_dump`` -> fresh-database
  -> ``pg_restore`` round trip (:mod:`civiccast.dr.restore_drill`'s
  ``run_postgres_restore_drill``) exactly like the SQLite path, plus the
  Postgres-only checks: installed extensions, sequence STATE (name +
  ``last_value`` + ``is_called``, not just names), constraint and index
  definitions compared via SAME-SERVER CANONICALIZATION (both sides
  re-deparsed through the restored database's own Postgres, not a
  text-normalization heuristic -- see ``_canonicalize_defs``), table grants,
  and the backup manifest's cluster-global-role capture (``globals.sql``,
  presence/non-empty on the same-cluster drill). A SEPARATE function,
  ``run_postgres_cold_standby_drill``, proves those roles are actually
  RESTORABLE (not merely captured) by replaying ``globals.sql`` onto an
  independently fresh second cluster and comparing role attributes and
  memberships by outcome, then restoring WITH ownership and comparing
  database/schema/table/sequence owners and grants -- the same-cluster
  drill structurally cannot prove this, because its restore target lives on
  the source's own cluster, where every owner/grantee role already exists.
  The proof is ``tests/dr/test_postgres_restore.py``: end-to-end drills
  against real ``postgres:17`` testcontainers (two, for the cold-standby
  drill), with negative controls (a post-restore row mutation must be
  DETECTED by the checksum comparison; a corrupted backup artifact must make
  ``run_postgres_restore`` raise; a corrupted globals capture, a tampered
  role attribute, and a revoked grant must each be DETECTED by the
  cold-standby drill). It follows the same testcontainers + gating pattern
  as ``tests/schedule/test_real_postgres.py`` and
  ``tests/dr/test_postgres_backup.py`` (``CIVICCAST_RUN_POSTGRES_TESTS=1``
  forces a hard failure instead of a skip when Docker is unavailable). CI
  (Linux + Docker) always exercises this path
  (# claim:ws2-postgres-backup-capture). **No machine used to develop
  this feature had Docker installed**, so no claim is made here about local
  execution — see
  ``.agent-runs/native-windows/ws2-postgres-restore/evidence/README.md``
  for the CI-run proof plan and the recorded run URL. The cold-standby
  drill has no operator-facing CLI entry point yet -- that wiring is
  explicit follow-up, not this pass.
* **The SQLite path — the default managed-storage deployment — is executed
  for real on every machine, with falsification**
  (# claim:ws2-sqlite-restore-falsification), as a plain part of the test
  suite.
* **Crash-recovery drill covers the daemon auto-restart path only** in this
  rung: a real OS child process is started under a real ``EgressDaemon``,
  killed, and the drill proves the daemon relaunches it. The "recording
  finalization interrupted mid-settle recovers on next scan" scenario named
  in the roadmap is NOT built in this pass — seeding a realistic
  ``LiveSession``/``RecordingTarget``/CDN-adapter fixture is a meaningfully
  larger effort than fit this rung. This is an explicit honest red, not a
  silent gap: see the module docstring of :mod:`civiccast.dr.crash_drill`.
* **Multi-machine hot failover is NOT 0.5.0.** CivicCast stations are a
  single-box reality. A second-box *cold standby* restore now has a
  dedicated proof function,
  :func:`civiccast.dr.restore_drill.run_postgres_cold_standby_drill` --
  role restorability, ownership, and grants checked by comparison against
  an independently fresh cluster, not just a restore-drill rerun -- but an
  operator-facing CLI entry point for it is follow-up, not this pass. There
  is no live replication.
* **Real-hardware power-loss drills are beta**, out of scope here.
"""

from __future__ import annotations
