# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CLI entry point the native NSIS hook set invokes for a journaled upgrade.

The NSIS macros in ``nsis-hooks-native.nsh`` call this as a thin shell — all
the logic lives in the tested engine (:mod:`civiccast.native.upgrade`), never
in NSIS script. Invocation::

    python -m civiccast.native.upgrade \\
        --old-version 1.0.0-rc15 --new-version 1.0.0-rc16 \\
        --install-root "C:\\Program Files\\CivicCast (Native)" \\
        --state-root  "C:\\ProgramData\\CivicCast\\upgrade" \\
        --database-url postgresql://... --owner-run-id <run-id> \\
        --payload-source "<nsis staging dir>"

Exit codes map to the terminal journal phase so the installer branches on a
number, not stderr text:

    0  COMPLETE                 (committed)
    10 ROLLED_BACK              (clean rollback; old version healthy)
    11 FRESH_INSTALL            (no installed product: the engine is not
                                 applicable; any preserved data root is adopted)
    12 SAME_VERSION_NO_OP       (installed product already at this version:
                                 no migration between a version and itself)
    20 HALTED_RESTORE_FAILED    (operator recovery document emitted)
    30 REFUSED_NON_RESTORABLE   (declared non-restorable; needs operator ack)
    31 REFUSED_STALE_JOURNAL    (a PREVIOUS run ended terminally and its journal
                                 is preserved; THIS run did nothing -- move or
                                 archive the journal to retry)
    40 unexpected error         (programming/environment fault)

11 and 12 come from the ROUTING decision (chain K/K2,
:mod:`civiccast.native.upgrade.routing`), which runs BEFORE any seam is built
and before any DB touch. Neither is a failure; both continue the install.

The argument parsing, seam assembly, and exit-code mapping here are unit-tested
(``tests/native/test_upgrade_cli.py``). The THREE service-control seams (drain
writers, maintenance/read-only health gate, service stop) are resolved through
:func:`_resolve_service_control_seams`, which WP-4 wired to the real production
callables in :mod:`civiccast.native.upgrade.service_control` (SCM start/stop,
the D7 control-pipe status read, the ``/health`` maintenance attestation, and
the WS2 snapshot-equality quiescence proof). Resolving the seams is inert
(touches no Win32/Postgres); the real OS/DB calls fire only when a seam is
INVOKED on the elevated install host (the WP-5 live matrix).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from civiccast.native.upgrade.models import (
    BackupRef,
    UpgradeContext,
    UpgradePhase,
    UpgradePlan,
    UpgradeSeams,
)
from civiccast.native.upgrade.orchestrator import run_upgrade
from civiccast.native.upgrade.pg_lifecycle import attach_pg_lifecycle
from civiccast.native.upgrade.routing import (
    UpgradeRoute,
    decide_route,
    default_installed_product_probe,
)
from civiccast.native.upgrade.seams import (
    adapt_flat_installer_layout,
    build_default_seams,
    default_expected_schema_head,
)
from civiccast.native.upgrade.service_control import resolve_service_control_seams

_EXIT_CODES: dict[UpgradePhase, int] = {
    UpgradePhase.COMPLETE: 0,
    UpgradePhase.ROLLED_BACK: 10,
    UpgradePhase.HALTED_RESTORE_FAILED: 20,
    UpgradePhase.REFUSED_NON_RESTORABLE: 30,
    # <installer-path-audit BL-06> A PREVIOUS run's terminal journal. Its own
    # code because the old behaviour returned the STALE journal's phase --
    # usually ROLLED_BACK, i.e. exit 10 -- which nsis-hooks-bootstrap.nsh
    # (post-#143) turns into a fatal 124 telling the operator to "re-run setup
    # after resolving the cause". Every re-run returned 10 again. Forever.
    # Whatever was fixed. Nothing deletes this journal:
    # %ProgramData%\CivicCast is preserved by uninstall BY DESIGN, so even
    # uninstall/reinstall did not clear it. 31 is the next free number in the
    # engine's own low band (0/10/11/12/20/30/40).
    UpgradePhase.REFUSED_STALE_JOURNAL: 31,
}

#: Exit codes for the runs that never enter the D3 sequence at all (chain
#: K/K2). Deliberately in the same low band as the phase codes above and
#: deliberately NOT 0: the hook must be able to tell "committed an upgrade"
#: apart from "decided there was no upgrade to run", because the two produce
#: different operator-facing text and different install-log breadcrumbs.
#: Neither is a failure -- both continue the install.
#: ``tests/policy/test_native_installer_identity.py`` cross-reads this dict
#: and ``_EXIT_CODES`` against the NSIS ladder in both directions, so a code
#: added here without a hook branch (or vice versa) fails the build.
_ROUTE_EXIT_CODES: dict[UpgradeRoute, int] = {
    UpgradeRoute.FRESH_INSTALL: 11,
    UpgradeRoute.SAME_VERSION_NO_OP: 12,
    # <installer-path-audit MA-05> Unlike 11 and 12 this one IS a refusal: a
    # newer product is installed and this setup would drive the database
    # backwards. Its own code so the hook can abort with an accurate sentence
    # rather than continuing an install that must not proceed.
    UpgradeRoute.REFUSED_DOWNGRADE: 13,
}

# Sibling to journal.JOURNAL_FILENAME ("upgrade-journal.json") under the same
# state root (beta BLOCKER #51). nsExec::ExecToLog (the NSIS macro that
# invokes this CLI) only captures this process's stdout/stderr into NSIS's
# OWN detail log, which is invisible in a silent install and lost entirely if
# the process is killed -- exactly what made Sandbox run 13's update-path
# timeout undiagnosable. This file is durable, append-mode, and created
# BEFORE any DB touch (the very first thing main() does after parsing args),
# so even a crash before the engine's first seam call leaves evidence.
ENGINE_LOG_FILENAME = "upgrade-engine.log"


def engine_log_path(state_root: str | Path) -> Path:
    """The durable D3 engine log path for ``state_root``."""

    return Path(state_root) / ENGINE_LOG_FILENAME


def _open_engine_log(state_root: str) -> Path:
    """Ensure ``state_root`` exists and return the engine log path.

    Called BEFORE the upgrade context/seams are built -- i.e. before any DB
    touch -- so an early crash (even one that predates ``UpgradeContext``
    construction) still lands a log file an operator can find.
    """

    path = engine_log_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _redact_database_url(text: str, database_url: str) -> str:
    """Strip any literal occurrence of ``database_url`` (which carries the
    connection password) out of ``text`` before it is written to the durable
    engine log. Defense in depth: no call below intentionally logs
    ``database_url``, but an exception's message or traceback COULD embed it
    (e.g. a driver's connection-string parse error) -- this log's whole
    purpose is to be readable by whoever is diagnosing a failure, and it must
    never carry the credential to do that. A blank/empty ``database_url`` is
    left alone (``str.replace(text, "", x)`` would otherwise splice ``x``
    between every character)."""

    if not database_url:
        return text
    return text.replace(database_url, "<database-url-redacted>")


def _log_engine_event(log_path: Path, database_url: str, message: str) -> None:
    """Append one UTC-timestamped, redacted line (or block) to the engine
    log. Never raises: a failure to write this diagnostic file must not mask
    or replace the real upgrade outcome."""

    timestamp = datetime.now(UTC).isoformat()
    body = _redact_database_url(message, database_url)
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {body}\n")
    except OSError:  # pragma: no cover - best-effort diagnostic, never fatal
        pass


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m civiccast.native.upgrade",
        description="Run the journaled CivicCast (Native) install/upgrade engine (spec D3).",
    )
    parser.add_argument("--old-version", required=True)
    parser.add_argument("--new-version", required=True)
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--owner-run-id", required=True)
    parser.add_argument("--payload-source", required=True)
    parser.add_argument(
        "--flat-installer-layout",
        action="store_true",
        help="Use the NSIS product's already-staged <install-root>\\runtime payload "
        "instead of the generic app/<version> junction layout.",
    )
    parser.add_argument(
        "--migration-non-restorable",
        action="store_true",
        help="Declare this release's migration cannot restore-roll-back (D3). "
        "Requires --operator-ack or the upgrade is refused.",
    )
    parser.add_argument("--operator-ack", default=None)
    return parser


def installed_product_probe() -> bool | None:
    """Is a real CivicCast (Native) product installed on this machine?

    A module-level seam (same shape as :func:`_resolve_service_control_seams`)
    so the routing decision can be exercised end to end under a fake, and so
    the ONE place the product-existence question is asked on this path is
    named and greppable. Delegates to
    :func:`civiccast.native.upgrade.routing.default_installed_product_probe`,
    which answers it from SCM registration of the supervisor service -- see
    that module's docstring for why every other signal is unusable here.
    """

    return default_installed_product_probe()


def _resolve_service_control_seams(
    context: UpgradeContext,
    *,
    expected_version: str | None = None,
    expected_schema_head: str | None = None,
) -> tuple[Callable[[], bool], Callable[[], bool], Callable[[], None]]:
    """Return ``(drain_and_verify_quiescence, health_gate, stop_service)`` bound
    to the real production callables (WP-4).

    Delegates to :func:`civiccast.native.upgrade.service_control.resolve_service_control_seams`:

    * drain_and_verify_quiescence -> confirm the writer-capable control plane
      drained (D7 control-pipe ``status`` -> ``workers_permitted`` False; the
      held D7a interlock does the draining, Postgres stays up) then prove
      quiescence via WS2 ``snapshot_tables`` equality across a settle interval.
    * health_gate -> SCM-start the service (interlock held -> maintenance/
      read-only boot) and poll ``check_control_plane_maintenance_ready`` green.
    * stop_service -> SCM stop of the CivicCast supervisor service.

    Resolving is inert (no Win32/Postgres); the real OS/DB calls fire only when
    a seam is INVOKED on the elevated install host (the WP-5 live matrix)."""

    return resolve_service_control_seams(
        context,
        expected_version=expected_version,
        expected_schema_head=expected_schema_head,
    )


# ---------------------------------------------------------------------------
# PostgreSQL client executables (Sandbox run 22 / run 16 defect)
# ---------------------------------------------------------------------------

#: The four PostgreSQL client executables D3 step 3 (BACKUP_VERIFIED) shells
#: out to, through :mod:`civiccast.dr.backup`: ``pg_dump`` (the pre-upgrade
#: dump), ``pg_dumpall`` (cluster globals), and ``pg_restore``/``psql`` (the
#: restore-drill spot check, and ``pg_restore`` again on the rollback path).
#:
#: Before this was wired, ``__main__`` called :func:`build_default_seams`
#: without any of the four ``*_command`` arguments, so ``dr/backup.py`` fell
#: back to its BARE-NAME defaults (``["pg_dump"]`` etc.) and Windows resolved
#: them through PATH. The installer writes no PATH entry and these ship only
#: inside the staged native-server-binaries pack tree, so on every real
#: machine step 3 raised ``[WinError 2] The system cannot find the file
#: specified`` and the engine rolled back -- exit 10, which the NSIS hook set
#: treats as a NON-failure. A version-changing upgrade therefore silently did
#: not happen while setup reported success (Sandbox run 22, 267 ms,
#: ``backups/pre-1.0.0-rc15/`` created and empty).
_PG_CLIENT_EXECUTABLES: tuple[str, ...] = (
    "pg_dump.exe",
    "pg_dumpall.exe",
    "pg_restore.exe",
    "psql.exe",
)


class PgClientBinariesMissingError(RuntimeError):
    """A PostgreSQL client executable D3 step 3 needs is not on disk.

    Raised INSTEAD of falling back to a bare name. The bare-name fallback is
    exactly what hid the wiring defect across two full sandbox runs: the
    spawn failed with a filename-less ``WinError 2``, which reads like an
    environment problem rather than a missing argument.
    """


def _resolve_pg_client_commands(context: UpgradeContext) -> dict[str, str]:
    """Absolute paths to the four client executables, keyed by file name.

    Resolved the SAME way ``pg_ctl.exe`` already is
    (:func:`civiccast.native.upgrade.pg_lifecycle.derive_pg_lifecycle_paths`):
    from :func:`civiccast.native.provision.__main__.resolve_provision_paths`'s
    ``initdb_path``, the single source of truth for the
    ``<install_root>\\packs\\native-server-binaries\\payload\\bin\\``
    convention. All five executables are members of the same signed
    ``native-server-binaries`` pack, which the NSIS bootstrap extracts and
    D2-verifies (``--civiccast-verify-pack-tree``) BEFORE it invokes this
    engine -- so no second binary-location convention is invented here.

    String-built (``rsplit`` on a backslash), not ``pathlib``, for the same
    reason ``resolve_provision_paths`` gives for building its own paths as
    strings: the Windows-style path must come out identical regardless of the
    host OS running this module's tests.

    Purely inert -- derives strings, touches no disk. Existence is proven by
    :func:`_require_pg_client_binaries` at the step that needs them.
    """

    from civiccast.native.provision.__main__ import resolve_provision_paths

    initdb_path = resolve_provision_paths(install_root=context.install_root).initdb_path
    bin_dir = initdb_path.rsplit("\\", 1)[0]
    return {name: f"{bin_dir}\\{name}" for name in _PG_CLIENT_EXECUTABLES}


def _require_pg_client_binaries(commands: Mapping[str, str]) -> None:
    """Raise :class:`PgClientBinariesMissingError` naming the D3 step and every
    absolute path that is not present."""

    missing = [
        commands[name] for name in _PG_CLIENT_EXECUTABLES if not Path(commands[name]).is_file()
    ]
    if not missing:
        return
    raise PgClientBinariesMissingError(
        "D3 step 3 (BACKUP_VERIFIED) cannot run: the PostgreSQL client "
        "executable(s) the pre-upgrade backup and its restore drill shell out "
        "to are not present in the installed native-server-binaries pack "
        "tree: " + "; ".join(missing) + ". The NSIS bootstrap extracts and "
        "D2-verifies that tree before this engine runs, so an absent path "
        "here means the extracted pack is incomplete or was removed after "
        "verification -- reinstall rather than retry. Refusing to fall back "
        "to a bare command name resolved through PATH."
    )


def _guard_pg_client_binaries(seams: UpgradeSeams, commands: Mapping[str, str]) -> UpgradeSeams:
    """Return ``seams`` with ``backup`` fronted by the existence check.

    Checked at INVOCATION of step 3, not at seam-assembly time, so the
    phase-0 refusal path (REFUSED_NON_RESTORABLE -> exit 30, decided before
    any seam is called) still reaches its own documented exit code on a
    machine whose pack tree is incomplete. Step 3 is the first step that
    needs any of the four, and it runs before ANY mutation -- so this is also
    the earliest point at which failing is meaningful.

    Wrapping is inert; nothing is checked until the seam is invoked.
    """

    inner = seams.backup

    def _guarded_backup(backup_dir: str) -> BackupRef:
        _require_pg_client_binaries(commands)
        return inner(backup_dir)

    return dataclasses.replace(seams, backup=_guarded_backup)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    # Durable tee log (beta BLOCKER #51): opened/created BEFORE any DB touch
    # -- before UpgradeContext/seams are even built -- so even a crash this
    # early leaves evidence under state_root, unlike nsExec::ExecToLog's
    # capture (invisible in silent installs, lost on a kill).
    log_path = _open_engine_log(args.state_root)
    _log_engine_event(
        log_path,
        args.database_url,
        f"upgrade engine starting: old={args.old_version} new={args.new_version}",
    )

    # ROUTE FIRST (chain K/K2), before the seams are built and before ANY DB
    # touch. Real hardware R7, 2026-08-01: a machine with no installed product
    # -- 0 ARP entries, no CivicCastSupervisor service, no install directory --
    # but with the ProgramData data root and the HKLM values that uninstall
    # deliberately preserves was routed into this engine as an "upgrade to
    # 1.0.0-rc15 from 1.0.0-rc15". The gate that let that happen keyed on those
    # preserved registry values and lived in NSIS; it now lives here, keyed on
    # whether a product actually EXISTS. See civiccast/native/upgrade/routing.py
    # for the signal choice and why every alternative is unusable.
    decision = decide_route(
        old_version=args.old_version,
        new_version=args.new_version,
        database_url=args.database_url,
        installed_product=installed_product_probe(),
        state_root=args.state_root,
    )
    _log_engine_event(
        log_path,
        args.database_url,
        f"route: {decision.route.value} -- {decision.reason}",
    )
    if decision.route is not UpgradeRoute.UPGRADE:
        # stderr, not just the durable log: nsExec::ExecToLog tees this into
        # the installer's own detail log, so the operator-facing reason lands
        # in install-progress.log next to the step breadcrumb.
        sys.stderr.write(f"upgrade outcome: {decision.route.value}\n")
        sys.stderr.write(f"{decision.reason}\n")
        return _ROUTE_EXIT_CODES[decision.route]

    plan = UpgradePlan(
        old_version=args.old_version,
        new_version=args.new_version,
        migration_restorable=not args.migration_non_restorable,
        operator_ack=args.operator_ack,
    )
    context = UpgradeContext(
        install_root=args.install_root,
        state_root=args.state_root,
        database_url=args.database_url,
        owner_run_id=args.owner_run_id,
    )

    # Exit-code contract: 40 covers EVERY unexpected error, including
    # exceptions the engine never anticipated. Without this, an uncaught
    # exception escapes as the interpreter's exit 1 and the installer's
    # fault branch reports a code outside the documented contract (observed
    # live in Sandbox matrix row 1, 2026-07-30: empty --database-url ->
    # SQLAlchemy ArgumentError -> exit 1). The traceback still goes to
    # stderr so the installer log keeps the full diagnosis; it is now ALSO
    # teed (redacted) into the durable engine log above.
    try:
        # <installer-path-audit BL-04> The maintenance health gate is the LAST
        # gate before COMPLETE / exit 0, and its attestation names no version,
        # build identity or schema revision -- so a supervisor left running
        # from BEFORE the upgrade, in maintenance mode precisely because the
        # interlock is held, certifies the upgrade by attesting to itself.
        # Bind the gate to what THIS payload is: the version being installed,
        # and the migration head this code's own alembic script directory
        # declares.
        expected_schema_head = default_expected_schema_head()()
        if expected_schema_head is None:
            # Review of PR #145: passing None through here SILENTLY UN-WIRED
            # the gate. `build_health_gate_seam` treats a None expected head
            # as "no identity gate requested" and returns True on the bare
            # maintenance attestation -- which is exactly BL-04, restored, on
            # any machine where the head could not be resolved. Refuse before
            # a single seam is assembled, so the failure is one clear line
            # rather than an upgrade that quietly loses two of its gates.
            reason = (
                "expected schema head unavailable: this payload's own migration head could "
                "not be resolved (alembic missing from the runtime, an unreadable script "
                "directory, or a branched migration graph). The upgrade engine refuses to "
                "run without it, because neither 'the migration landed' nor 'the new code is "
                "the process attesting health' can be checked."
            )
            _log_engine_event(log_path, args.database_url, f"upgrade outcome: refused -- {reason}")
            sys.stderr.write(f"upgrade outcome: refused\n{reason}\n")
            return 40
        drain, health, stop = _resolve_service_control_seams(
            context,
            expected_version=plan.new_version,
            expected_schema_head=expected_schema_head,
        )
        _log_engine_event(
            log_path,
            args.database_url,
            "health gate bound to "
            f"version={plan.new_version!r} expected_schema_head={expected_schema_head!r}",
        )
        # Sandbox run 22: without these four, dr/backup.py falls back to bare
        # command names resolved through PATH, and step 3 (BACKUP_VERIFIED)
        # dies with a filename-less WinError 2 on every real machine. The
        # resolved paths are teed into the durable engine log so the NEXT
        # diagnosis reads the attempted path instead of reconstructing it.
        pg_clients = _resolve_pg_client_commands(context)
        _log_engine_event(
            log_path,
            args.database_url,
            "resolved PostgreSQL client executables for D3 step 3: "
            + "; ".join(pg_clients[name] for name in _PG_CLIENT_EXECUTABLES),
        )
        seams = build_default_seams(
            context,
            payload_source=args.payload_source,
            drain_and_verify_quiescence=drain,
            health_gate=health,
            stop_service=stop,
            pg_dump_command=[pg_clients["pg_dump.exe"]],
            pg_dumpall_command=[pg_clients["pg_dumpall.exe"]],
            pg_restore_command=[pg_clients["pg_restore.exe"]],
            psql_command=[pg_clients["psql.exe"]],
        )
        seams = _guard_pg_client_binaries(seams, pg_clients)
        if args.flat_installer_layout:
            seams = adapt_flat_installer_layout(seams, context)
            _log_engine_event(
                log_path,
                args.database_url,
                "payload layout: flat installer runtime selected; no junction required",
            )
        # BLOCKER #49: the D3 chain runs BEFORE D4 provisioning, so an
        # upgrade/reinstall whose previous service is absent or deliberately
        # quiesced by PREINSTALL (and therefore postgres is stopped) would
        # otherwise fault uncaught at the engine's first DB touch
        # (schema_revision). attach_pg_lifecycle starts postgres only when the
        # service is absent or confirmed STOPPED, hands ownership back before
        # the maintenance health gate, and returns a stop callable this process
        # MUST run in a finally -- so postgres this process starts is never
        # left running for D4 provisioning to trip over.
        seams, stop_postgres_if_started = attach_pg_lifecycle(seams, context)
        try:
            outcome = run_upgrade(plan, context, seams)
        finally:
            stop_postgres_if_started()
    except Exception:
        traceback.print_exc()
        _log_engine_event(
            log_path,
            args.database_url,
            "upgrade outcome: unexpected fault\n" + traceback.format_exc(),
        )
        sys.stderr.write("upgrade outcome: unexpected fault\n")
        return 40
    sys.stderr.write(f"upgrade outcome: {outcome.phase.value}\n")
    outcome_message = f"upgrade outcome: {outcome.phase.value}"
    if outcome.journal.error:
        # Coordinator review of PR #143: this file used to log only the
        # phase name (e.g. "upgrade outcome: rolled_back") -- the NSIS hook's
        # exit==10 message points an operator at this log for "the exact
        # reason", but the reason (orchestrator._rollback's `reason=str(exc)`,
        # which funnels ANY operational step failure: drain, backup/restore-
        # drill, migrate, health gate) was never actually written here, only
        # into the journal JSON. Surface it in the durable, operator-readable
        # log too.
        outcome_message += f"\nreason: {outcome.journal.error}"
    _log_engine_event(log_path, args.database_url, outcome_message)
    return _EXIT_CODES.get(outcome.phase, 40)


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
