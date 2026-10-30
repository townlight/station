# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Default (real) seam implementations for the D3 upgrade engine.

The orchestrator is pure over :class:`~civiccast.native.upgrade.models.UpgradeSeams`;
this module builds the PRODUCTION bundle that wires each seam to the real
machinery already proven elsewhere in the tree:

* interlock  -> :mod:`civiccast.native.win_probes` (D7a, ws4)
* backup     -> :func:`civiccast.dr.backup.run_full_backup` + a restore-drill
                spot check via :func:`civiccast.dr.restore_drill.run_postgres_restore_drill`
                (WS2). Verification is "artifact hash + restore-drill spot check"
                exactly as D3 step 3 requires.
* restore    -> :func:`civiccast.dr.backup.run_postgres_restore` (WS2)
* migrate    -> ``alembic.command.upgrade(cfg, "head")`` against the live DB,
                using the same path resolution the startup schema check uses
                (:func:`civiccast.schema_check._alembic_runtime_paths`)
* schema rev -> :func:`civiccast.schema_check.read_db_revision`
* junction   -> :mod:`civiccast.native.upgrade.junction`

Three seams are service-control actions (drain writers, start the
maintenance/read-only health gate, stop the service). Those cross into the
Windows Service Control Manager + the running supervisor. WP-4 wired them to
real production callables in
:mod:`civiccast.native.upgrade.service_control` (SCM start/stop, the D7
control-pipe status read, the ``/health`` maintenance attestation, and the WS2
snapshot-equality quiescence proof). :func:`build_default_seams` therefore
REQUIRES the caller to pass those three callables rather than shipping a fake
default that would silently no-op a safety-critical step; ``__main__`` supplies
them via ``service_control.resolve_service_control_seams``. Their real OS/DB
round trips fire only on the elevated install host (the WP-5 live matrix).
"""

from __future__ import annotations

import dataclasses
import hashlib
import shutil
from collections.abc import Callable
from pathlib import Path

from civiccast.db.url import normalize_database_url
from civiccast.dr.backup import run_full_backup, run_postgres_restore
from civiccast.dr.models import BackupManifest
from civiccast.dr.restore_drill import run_postgres_restore_drill
from civiccast.native.upgrade import junction
from civiccast.native.upgrade.models import (
    BackupRef,
    UpgradeContext,
    UpgradeSeams,
)


def _manifest_blob_hash(manifest: BackupManifest) -> str:
    """A single sha256 over the backup set's per-file integrity hashes.

    The manifest's ``integrity`` block already carries a sha256 per shipped
    file (see :func:`civiccast.dr.backup.write_integrity_manifest`); folding
    them into one digest gives the whole backup a stable blob identity the
    journal can bind to and a later step can re-derive to detect tampering.
    """

    hasher = hashlib.sha256()
    for entry in sorted(manifest.integrity, key=lambda e: e.member):
        hasher.update(entry.member.encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(entry.sha256.encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def default_acquire_interlock(owner_run_id: str) -> Callable[[], None]:
    def _acquire() -> None:
        from civiccast.native.win_probes import take_interlock

        take_interlock(owner_run_id)

    return _acquire


def default_release_interlock(owner_run_id: str) -> Callable[[], None]:
    """Release the D7a interlock, but ONLY if this run is the one holding it.

    <installer-path-audit BL-05> The release used to be unconditional on both
    sides: the orchestrator called it on every rollback (including one caused
    by ``acquire_interlock`` itself failing) and ``release_interlock`` checked
    only that a record existed and was ``held``, never comparing
    ``owner_run_id``. So a second concurrent installer released the first
    one's interlock mid-migration. The orchestrator's phase guard is the other
    half of this fix.
    """

    def _release() -> None:
        from civiccast.native.win_probes import release_interlock

        release_interlock(owner_run_id=owner_run_id)

    return _release


def default_backup(
    context: UpgradeContext,
    *,
    media_root: Path | None = None,
    pg_dump_command: list[str] | None = None,
    pg_dumpall_command: list[str] | None = None,
    pg_restore_command: list[str] | None = None,
    psql_command: list[str] | None = None,
    command_database_url: str | None = None,
) -> Callable[[str], BackupRef]:
    """Real pre-upgrade backup: WS2 full backup + a restore-drill spot check.

    D3 step 3 requires the backup be VERIFIED "artifact hash + restore-drill
    spot check" BEFORE any mutation. This does both: takes the WS2 full backup
    (which itself writes + hashes an integrity manifest), then restores it into
    a throwaway database and asserts row/checksum/schema parity via
    :func:`run_postgres_restore_drill`. A failure of either raises (the
    orchestrator treats a raise or an unverified ref as a hard stop, so no
    mutation proceeds without a proven recovery point).

    ``command_database_url`` is the URL the ``pg_dump``/``pg_dumpall``/
    ``pg_restore``/``psql`` CLI tools parse for their own ``--host``/
    ``--port``/``--username`` argv (see :func:`civiccast.dr.backup.
    run_full_backup`'s own ``command_database_url`` doc) -- the in-container
    view when ``pg_dump_command``/etc. carry a ``docker exec`` prefix. It
    defaults to ``None``, in which case every call below defaults it right
    back to ``context.database_url`` (unchanged production behavior: one
    reachable Postgres, no container indirection). This parameter exists so
    a Postgres-container-backed test can reuse this REAL seam unmodified with
    the same ``docker exec`` pattern ``tests/dr/test_postgres_restore.py``
    already proves, instead of requiring Postgres client tools on the host
    running the test -- ``context.database_url`` stays the HOST-reachable URL
    every direct SQLAlchemy read in this closure and in :func:`default_migrate`/
    :func:`default_schema_revision` needs.
    """

    def _backup(backup_dir: str) -> BackupRef:
        dest = Path(backup_dir)
        manifest = run_full_backup(
            database_url=context.database_url,
            dest_dir=dest,
            media_root=media_root,
            command_database_url=command_database_url,
            pg_dump_command=pg_dump_command,
            pg_dumpall_command=pg_dumpall_command,
        )
        blob_hash = _manifest_blob_hash(manifest)

        restore_ok = False
        drill_errors: list[str] = []
        if manifest.engine == "postgres":
            # The pre-upgrade drill's "schema_ok" question is "does the
            # restored copy match what we actually dumped", NOT "does it
            # match the NEW code's migration head" -- migrate() has not run
            # yet, so on any release that ships a migration the latter is
            # always false (Gate A run 33681670855 root cause). Pass the
            # SOURCE database's own current revision -- read from the same
            # database this backup was just taken from, so it reflects the
            # exact pre-upgrade state the dump captured.
            from civiccast.schema_check import read_db_revision

            source_revision = read_db_revision(context.database_url)
            if source_revision is None:
                # Fail closed here rather than let run_postgres_restore_drill
                # silently fall back to its own default (expected_migration_
                # head()) -- that fallback exists for real DR-drill callers,
                # but here it would quietly reintroduce this exact fix's root
                # cause (comparing a pre-upgrade restore against the NEW
                # code's head) with no signal that it happened because the
                # SOURCE revision could not be read, rather than by design.
                restore_ok = False
                drill_errors = [
                    "could not read the source database's own current revision "
                    "before the restore-drill (schema_check.read_db_revision "
                    "returned None) -- refusing to fall back to comparing "
                    "against the code's migration head, which is the exact "
                    "false-negative Gate A run 33681670855 found"
                ]
            else:
                report = run_postgres_restore_drill(
                    backup_dir=dest,
                    manifest=manifest,
                    source_database_url=command_database_url or context.database_url,
                    verification_database_url=context.database_url,
                    pg_restore_command=pg_restore_command,
                    psql_command=psql_command,
                    expected_revision=source_revision,
                )
                restore_ok = report.ok
                if not restore_ok:
                    drill_errors = list(report.errors)
                    if not report.schema_ok:
                        drill_errors.append(
                            "schema_ok=False: restored revision "
                            f"{report.db_revision!r} != expected {report.expected_head!r}"
                        )
        else:
            # SQLite deployments verify via the same drill entry point in WS2;
            # the native product targets Postgres, so a non-postgres engine
            # here is unexpected — fail closed rather than claim a spot check
            # we did not run.
            restore_ok = False
            drill_errors = ["backup manifest engine is not 'postgres'; no restore-drill was run"]

        return BackupRef(
            backup_id=manifest.backup_id,
            backup_dir=str(dest),
            manifest_hash=blob_hash,
            db_artifact=manifest.db_artifact,
            verified=bool(manifest.integrity),
            restore_drill_ok=restore_ok,
            restore_drill_errors=drill_errors,
        )

    return _backup


def default_restore_backup(
    context: UpgradeContext,
    *,
    pg_restore_command: list[str] | None = None,
    psql_command: list[str] | None = None,
    command_database_url: str | None = None,
) -> Callable[[BackupRef], None]:
    """Restore the pre-upgrade backup INTO THE LIVE DATABASE.

    <installer-path-audit BL-01> This used to be a bare ``pg_restore`` into
    ``context.database_url`` -- the live database, which still holds every
    object in the dump PLUS whatever the partial migration added. The argv was

        pg_restore --host --port --username --dbname --no-owner
                   --exit-on-error --no-password

    and a grep for ``--clean`` / ``--if-exists`` / ``--create`` across
    ``civiccast/dr/*.py`` and ``civiccast/native/upgrade/*.py`` found none.
    Nothing dropped or recreated the target first. So on any post-mutation
    failure ``pg_restore`` replayed ``CREATE TABLE``, hit
    ``relation "..." already exists``, ``--exit-on-error`` exited nonzero,
    ``run_postgres_restore`` raised, and the orchestrator went to ``_halt``:
    the station got HALTED_RESTORE_FAILED / exit 20 / service stopped / a
    manual recovery document. **The clean-rollback outcome (exit 10) that
    PR #143 was written around was unreachable for every post-migration
    failure**, while two shipped comments asserted the opposite as established
    fact and reasoned from it.

    The fix restores into the drill's own verified path: drop and recreate the
    target database (``create_fresh_postgres_database`` with the live
    database's own name, under the D7a interlock this whole phase holds), then
    replay the dump into a genuinely empty database inside ONE transaction so a
    mid-replay failure cannot leave production half-clobbered.

    Two integrity gates run before any of that:

    * the artifact's own bytes are re-hashed against the backup manifest
      (``verify_backup_integrity``, installer-path audit MA-09) -- a truncated
      dump written after the manifest used to pass ``verified=True``, because
      ``verified`` was ``bool(manifest.integrity)``, a non-emptiness check;
    * the manifest must name the artifact this ``BackupRef`` points at.

    A failure of either raises BEFORE the target is dropped, so an unusable
    backup can never destroy a live database that still had its data.
    """

    def _restore(backup: BackupRef) -> None:
        from civiccast.dr.backup import (
            create_fresh_postgres_database,
            read_backup_manifest,
            verify_backup_integrity,
        )

        backup_dir = Path(backup.backup_dir)
        artifact = backup_dir / backup.db_artifact
        if not artifact.is_file():
            raise RuntimeError(
                f"refusing to drop the live database: the backup artifact {artifact} does not "
                "exist, so there would be nothing to restore from"
            )
        manifest = read_backup_manifest(backup_dir)
        errors = verify_backup_integrity(backup_dir, manifest)
        if errors:
            raise RuntimeError(
                "refusing to drop the live database: the pre-upgrade backup no longer matches "
                "its own integrity manifest (" + "; ".join(errors) + ")"
            )

        # THE CLI TOOLS' OWN VIEW OF THE SERVER, not this process's.
        #
        # `create_fresh_postgres_database` shells to `psql` and
        # `run_postgres_restore` shells to `pg_restore`; both parse this URL
        # for their `--host`/`--port`/`--username` argv. When those commands
        # carry a prefix that relocates them (the `docker exec` pattern the DR
        # suite and the D3 engine's own Postgres proof already use), the URL
        # they must parse is the server's view from INSIDE that container --
        # which is exactly what `command_database_url` is, and exactly what
        # `default_backup` above already threads through for `pg_dump` /
        # `pg_dumpall` / the drill.
        #
        # This seam did not accept it. `context.database_url` is the
        # HOST-reachable URL every direct SQLAlchemy read needs, so the
        # rollback handed `pg_restore` a host port while running it inside the
        # container: `connection to server at "localhost", port 32803 failed:
        # Connection refused`. `run_postgres_restore` raised, `_rollback` went
        # to `_halt`, and the run reported HALTED_RESTORE_FAILED -- i.e. BL-01's
        # own symptom, reproduced by BL-01's own fix. `None` in production
        # (one reachable Postgres, no container indirection) keeps behaviour
        # exactly as it was.
        command_url = command_database_url or context.database_url
        target_name = _database_name(command_url)
        recreated_url = create_fresh_postgres_database(
            database_url=command_url,
            database_name=target_name,
            psql_command=psql_command,
            allow_dropping_the_connection_url_database=True,
        )
        run_postgres_restore(
            artifact,
            recreated_url,
            pg_restore_command=pg_restore_command,
            single_transaction=True,
        )

    return _restore


def _database_name(database_url: str) -> str:
    """The database name a URL addresses, for the drop/recreate above."""

    from urllib.parse import urlsplit

    name = urlsplit(database_url).path.lstrip("/")
    if not name:
        raise RuntimeError(
            f"cannot determine which database to restore into: {database_url!r} names none"
        )
    return name


def default_lay_tree(
    context: UpgradeContext, *, payload_source: str | Path
) -> Callable[[str], str]:
    """Lay ``app\\<new>\\`` by copying the (already verified) payload tree in.

    This versioned-tree helper trusts verification performed by its caller;
    it does not authenticate a payload itself. The current bootstrap installer
    uses signed component packs: it stages them with --civiccast-stage-packs
    and verifies the extracted runtime against native-app-payload.ccpack with
    --civiccast-verify-pack-tree before invoking the upgrade engine. The former
    WP-5 staging task is closed; see next-cleanup.md for its disposition.
    """

    def _lay(new_version: str) -> str:
        target = Path(context.install_root) / "app" / new_version
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copytree(payload_source, target)
        return str(target.resolve())

    return _lay


def default_flip_junction(context: UpgradeContext) -> Callable[[str], None]:
    def _flip(target: str) -> None:
        junction.point_current_at(context.install_root, target)

    return _flip


def default_read_junction(context: UpgradeContext) -> Callable[[], str | None]:
    def _read() -> str | None:
        return junction.read_current_target(context.install_root)

    return _read


def adapt_flat_installer_layout(
    seams: UpgradeSeams,
    context: UpgradeContext,
) -> UpgradeSeams:
    """Model the payload layout the production NSIS installer actually uses.

    The bootstrap extracts and D2-verifies the live Python payload directly at
    ``<install_root>\\runtime`` before it starts D3.  Service registration and
    station activation also consume that flat path; they do not consume the
    generic engine's ``app\\<version>`` / ``current`` junction convention.

    Replacing only the three payload-selection seams keeps D3's safety-critical
    writer drain, verified backup/restore drill, migration, health gate, and
    journal intact.  The adapter deliberately refuses any target other than the
    verified flat runtime, so it cannot silently turn into a general no-op.
    """

    runtime = (Path(context.install_root) / "runtime").resolve()

    def _require_runtime() -> str:
        if not runtime.is_dir():
            raise RuntimeError(
                f"flat runtime payload is missing after installer D2 verification: {runtime}"
            )
        return str(runtime)

    def _select_runtime(target: str) -> None:
        expected = _require_runtime()
        if Path(target).resolve() != runtime:
            raise RuntimeError(
                "flat runtime payload selector refused an unexpected target: "
                f"expected {expected}, got {target}"
            )

    return dataclasses.replace(
        seams,
        read_junction=_require_runtime,
        lay_tree=lambda _new_version: _require_runtime(),
        flip_junction=_select_runtime,
        # <installer-path-audit MA-01> Say so, in the bundle and therefore in
        # the journal. Under this adapter read_junction, lay_tree and
        # flip_junction all return the SAME <install_root>\runtime string, so
        # previous_junction_target == new_junction_target and the rollback's
        # flip-back is a tautology -- the guard at _select_runtime compares the
        # argument against a value the same adapter produced and can never fire
        # on the rollback path. There is no old tree to revert to, and the
        # journal used to claim "junction/tree reverted" anyway. The
        # orchestrator now branches its rollback detail and its operator
        # recovery document on this flag instead of asserting a revert that did
        # not happen.
        filesystem_rollback=False,
    )


def default_migrate(context: UpgradeContext) -> Callable[[], None]:
    """Run ``alembic upgrade head`` against the live DB, in-process.

    Uses the same ini/script/version-location resolution the startup schema
    check uses (:func:`civiccast.schema_check._alembic_runtime_paths`), so the
    engine walks EVERY module's migration head exactly like a shell
    ``alembic upgrade head`` would, and is idempotent (already-applied
    revisions are skipped by alembic).
    """

    def _migrate() -> None:
        from alembic import command
        from alembic.config import Config

        from civiccast.schema_check import _alembic_runtime_paths

        ini, script_location, version_locations = _alembic_runtime_paths()
        cfg = Config(str(ini))
        cfg.set_main_option("script_location", str(script_location.resolve()))
        cfg.set_main_option(
            "version_locations",
            "\n".join(str(path.resolve()) for path in version_locations),
        )
        cfg.set_main_option("path_separator", "newline")
        # normalize_database_url: a bare `postgresql://` scheme maps to the
        # uninstalled psycopg2 dialect (ADR 0008 ships psycopg v3 only) --
        # beta BLOCKER #51. alembic's env.py reads this back from cfg, so
        # normalizing here covers the whole `alembic upgrade head` run.
        cfg.set_main_option("sqlalchemy.url", normalize_database_url(context.database_url))
        command.upgrade(cfg, "head")

    return _migrate


def default_expected_schema_head() -> Callable[[], str | None]:
    """The migration head THIS payload's own alembic script directory declares.

    <installer-path-audit BL-03> The orchestrator compares
    ``post_schema_revision`` to this after ``migrate()``. It is deliberately a
    property of the SHIPPED CODE (read through the same
    ``schema_check._alembic_runtime_paths`` resolution ``default_migrate``
    itself uses), not of the database and not of anything the running control
    plane reports -- so "the migration landed" is judged against the payload's
    own expectation rather than against a value the migration itself produced.

    Returns None if the head cannot be resolved (e.g. a branched graph, which
    ``expected_migration_head`` raises on). The orchestrator records that the
    assertion was UNAVAILABLE rather than treating an unresolvable head as a
    pass.
    """

    def _head() -> str | None:
        from civiccast.schema_check import expected_migration_head

        try:
            return expected_migration_head()
        except (ImportError, OSError):
            # NARROW ON PURPOSE (review of PR #145). A bare ``except
            # Exception: return None`` here was FAIL-OPEN twice over: it
            # turned BL-03's post-migration assertion into a journal string,
            # and it un-wired BL-04's identity gate (``build_health_gate_seam``
            # treats a ``None`` expected head as "no identity gate requested"
            # and returns True on the bare maintenance attestation).
            #
            # These two are the failures where "unavailable" is the honest
            # answer and the caller's own refusal carries the better message:
            # alembic missing from the payload (ImportError) and an unreadable
            # ini / script directory (OSError). Everything else -- notably the
            # ``RuntimeError`` ``expected_migration_head`` raises for a
            # BRANCHED migration graph -- PROPAGATES, so the orchestrator's
            # funnel records that specific reason ("Expected exactly one
            # migration head, found [...]") instead of laundering it into a
            # generic one.
            #
            # Either way the upgrade now HALTS: the orchestrator refuses on a
            # ``None`` head rather than committing, and ``__main__`` refuses to
            # assemble seams at all when the head cannot be resolved.
            return None

    return _head


def default_schema_revision(context: UpgradeContext) -> Callable[[], str | None]:
    def _revision() -> str | None:
        from civiccast.schema_check import read_db_revision

        return read_db_revision(context.database_url)

    return _revision


def build_default_seams(
    context: UpgradeContext,
    *,
    payload_source: str | Path,
    drain_and_verify_quiescence: Callable[[], bool],
    health_gate: Callable[[], bool],
    stop_service: Callable[[], None],
    media_root: Path | None = None,
    pg_dump_command: list[str] | None = None,
    pg_dumpall_command: list[str] | None = None,
    pg_restore_command: list[str] | None = None,
    psql_command: list[str] | None = None,
    command_database_url: str | None = None,
) -> UpgradeSeams:
    """Assemble the production seam bundle for ``context``.

    The three service-control seams (``drain_and_verify_quiescence``,
    ``health_gate``, ``stop_service``) are REQUIRED arguments, not defaulted:
    they cross into the SCM + the running supervisor. Requiring them keeps this
    function honest — it never ships a silent no-op for a safety-critical step.
    WP-4 wired them in :mod:`civiccast.native.upgrade.service_control`
    (``resolve_service_control_seams``): SCM start for the maintenance health
    gate, SCM stop for the halt path, and a control-pipe drain-confirm + WS2
    snapshot-equality check for drain; ``__main__`` passes them here.

    ``command_database_url`` passes straight through to :func:`default_backup`
    (see its own docstring) -- ``None`` in every production call, which keeps
    behavior unchanged (one reachable Postgres, no container indirection).
    """

    return UpgradeSeams(
        acquire_interlock=default_acquire_interlock(context.owner_run_id),
        release_interlock=default_release_interlock(context.owner_run_id),
        drain_and_verify_quiescence=drain_and_verify_quiescence,
        backup=default_backup(
            context,
            media_root=media_root,
            pg_dump_command=pg_dump_command,
            pg_dumpall_command=pg_dumpall_command,
            pg_restore_command=pg_restore_command,
            psql_command=psql_command,
            command_database_url=command_database_url,
        ),
        # BOTH of the restore seam's own tool arguments, and the URL those
        # tools parse. `psql_command` was missing here as well: the BL-01
        # rollback newly calls `create_fresh_postgres_database`, which shells
        # to `psql`, and without this it fell back to `dr/backup.py`'s BARE
        # NAME default and resolved `psql` through PATH. The installer writes
        # no PATH entry and these ship only inside the staged
        # native-server-binaries pack, so on a real station that is a
        # filename-less WinError 2 -- the exact Sandbox run 22 defect
        # `_resolve_pg_client_commands` exists to prevent, reintroduced on the
        # one path that runs when an upgrade is already going wrong.
        restore_backup=default_restore_backup(
            context,
            pg_restore_command=pg_restore_command,
            psql_command=psql_command,
            command_database_url=command_database_url,
        ),
        lay_tree=default_lay_tree(context, payload_source=payload_source),
        flip_junction=default_flip_junction(context),
        read_junction=default_read_junction(context),
        migrate=default_migrate(context),
        health_gate=health_gate,
        schema_revision=default_schema_revision(context),
        stop_service=stop_service,
        expected_schema_head=default_expected_schema_head(),
    )
