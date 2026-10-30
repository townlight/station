# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""D3's SCOPED postgres lifecycle (BLOCKER #49).

Live-proven defect (``.agent-runs/native-windows/ws5-installer/evidence/
gauntlet-run11/``, row-4b): the documented update path (uninstall preserving
the DB cluster + HKLM ``DatabaseUrl``/``InstalledVersion``, then reinstall)
runs the D3 upgrade engine (:mod:`civiccast.native.upgrade`) BEFORE D4
provisioning in the NSIS chain. Uninstall removes the ``CivicCastSupervisor``
service, so nothing is running postgres when D3 starts; the engine's very
first DB touch -- ``seams.schema_revision()``, called immediately after the
phase-0 journal write in :func:`civiccast.native.upgrade.orchestrator.
run_upgrade` -- faults with an uncaught connection error, mapping to engine
exit 40 / installer exit 115.

This module wraps exactly that first DB-touching seam with a narrow decision,
confined to the CLI entry path (:mod:`civiccast.native.upgrade.__main__`) so
``orchestrator.py`` stays PURE (never touches Windows/Postgres/subprocess
itself -- see that module's docstring):

* reachable                              -> do nothing; never stop what we
                                             did not start (not ours to
                                             manage).
* unreachable, CivicCastSupervisor ABSENT from the SCM, or registered but
  definitively STOPPED (:func:`civiccast.native.upgrade.service_control.
  _real_service_stopped_probe`) -> start postgres against the preserved
  data dir via the ONE blessed pg_ctl wrapper
  (:mod:`civiccast.native.supervisor.children`'s ``postgres_child_spec`` /
  ``graceful_stop_action`` -- this module introduces NO second pg_ctl
  call-site, it only supplies the ``subprocess.run`` children.py
  deliberately does not), run the engine, and stop postgres again in a
  ``finally`` (never left running for D4 provisioning to trip over).
* unreachable, service running/transitional (or STOPPED could not be confirmed) ->
  fail closed (unchanged behavior) with a message naming the actual
  condition instead of an uncaught traceback.

Data dir / pg_ctl path / host / port are derived exactly the way D4
provisioning does (:func:`civiccast.native.provision.__main__.
resolve_provision_paths`) plus the database_url the rest of the engine
already connects to -- no new path or endpoint convention is invented here.

The lifecycle wraps every resumable DB boundary (schema revision, drain,
backup, migrate, and rollback restore) plus the maintenance health gate, where
scoped pg_ctl ownership is handed back to the supervisor.

Every real primitive here (subprocess pg_ctl invocation, SQLAlchemy
connection, SCM query) is lazily imported/executed only when the wrapped
seam actually runs, matching the house pattern in ``seams.py`` and
``service_control.py``; the DECISION logic (:func:`wrap_schema_revision`) is
pure over injected callables so it is fully unit-tested without Windows,
Postgres, or the SCM.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from civiccast.db import connect_options
from civiccast.db.guarded_connect import DatabaseMissingError as _SharedDatabaseMissingError
from civiccast.db.guarded_connect import classify_missing_database, run_bounded
from civiccast.db.url import normalize_database_url
from civiccast.native.pg_ctl_exec import run_pg_ctl_argv
from civiccast.native.pgdata_acl import PgDataAclError, normalize_pgdata_acl
from civiccast.native.supervisor.children import (
    DEFAULT_GRACEFUL_STOP_DEADLINE_SECONDS,
    POSTGRES_READY_BUDGET_SECONDS,
    ChildSpec,
    graceful_stop_action,
    postgres_child_spec,
)
from civiccast.native.upgrade.models import BackupRef, UpgradeContext, UpgradeSeams

DatabaseReachableFn = Callable[[], bool]
ServiceRegisteredProbe = Callable[[], bool | None]
StartPostgresFn = Callable[[], None]
StopPostgresFn = Callable[[], None]
SchemaRevisionFn = Callable[[], str | None]
HealthGateFn = Callable[[], bool]
RestoreBackupFn = Callable[[BackupRef], None]

# Margin over children.py's own spec-carried budgets: pg_ctl's own ``-w``
# wait (start) / the D5 graceful-stop deadline (stop) should elapse and
# return an error before OUR subprocess timeout ever fires, so a real pg_ctl
# failure is reported as "pg_ctl start/stop failed", never a generic
# ``subprocess.TimeoutExpired``.
_START_TIMEOUT_SECONDS = POSTGRES_READY_BUDGET_SECONDS + 15.0
_STOP_TIMEOUT_SECONDS = DEFAULT_GRACEFUL_STOP_DEADLINE_SECONDS + 15.0
_STDERR_TAIL_CHARS = 2000

FAIL_CLOSED_DETAIL = (
    "postgres is unreachable and the CivicCastSupervisor service is "
    "not confirmed stopped (service may own postgres but database is unreachable) -- refusing to "
    "start postgres out from under a service that may already own it. "
    "Verify the CivicCastSupervisor service state and the postgres data "
    "directory manually, then retry the upgrade."
)


class PostgresLifecycleError(RuntimeError):
    """Fail-loud fault from the D3 scoped postgres lifecycle: the fail-closed
    refusal above, or a real pg_ctl start/stop failure. Messages are built
    only from pg_ctl argv/exit-code/stderr and filesystem paths -- NEVER the
    database_url (which carries the connection password)."""


class DatabaseMissingError(_SharedDatabaseMissingError, PostgresLifecycleError):
    """BLOCKER #52: postgres answered, but ``context.database_url``'s
    TARGET DATABASE itself does not exist (D4 provisioning never ran
    ``CREATE DATABASE`` -- see :mod:`civiccast.native.provision.seams`'s
    ``ensure_database`` seam, added alongside this fix). This is NOT a
    "not-yet-ready, keep waiting" condition: postgres is up and answering,
    so starting postgres (already running) or retrying can never fix it.
    Raised immediately by :func:`real_database_reachable` -- never treated
    as ordinary unreachability, never funneled into
    :func:`wrap_schema_revision`'s start-postgres-then-retry decision.

    Task #55: subclasses BOTH the module-local :class:`PostgresLifecycleError`
    (unchanged prior behavior/hierarchy) AND the shared
    :class:`civiccast.db.guarded_connect.DatabaseMissingError` (task #55's
    consolidated home for this classification, also used by
    :func:`civiccast.schema_check.read_db_revision`), so existing ``except
    PostgresLifecycleError`` handling and any new ``except
    guarded_connect.DatabaseMissingError`` handling both still catch this."""


@dataclass
class PgLifecycleState:
    """Shared, per-run mutable state between the wrapped ``schema_revision``
    seam (decides whether to start postgres) and the outer ``finally`` block
    (stops it iff THIS run started it). ``checked`` guards against
    re-deciding on schema_revision's SECOND call (post-migration revision)."""

    checked: bool = False
    started_by_us: bool = False


@dataclass(frozen=True)
class PgLifecyclePaths:
    pg_ctl_path: str
    data_dir: str
    host: str
    port: int


def _tail(text: str, limit: int = _STDERR_TAIL_CHARS) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


# ---------------------------------------------------------------------------
# Path derivation -- reuses D4 provisioning's convention, invents nothing new
# ---------------------------------------------------------------------------


def derive_pg_lifecycle_paths(context: UpgradeContext) -> PgLifecyclePaths:
    """Derive pg_ctl path / data dir / host / port the SAME way D4
    provisioning does.

    :func:`civiccast.native.provision.__main__.resolve_provision_paths` is
    the existing, single source of truth for the
    ``<ProgramData>\\CivicCast\\data\\pgdata`` data-dir convention and the
    ``<install_root>\\packs\\native-server-binaries\\payload\\bin\\`` staged
    server-binaries bin directory (``initdb.exe`` is resolved there today,
    D2-verified before D3 runs per the brief); ``pg_ctl.exe`` is D2-verified
    into the SAME bin directory, so its path is derived as ``initdb_path``'s
    sibling rather than inventing a second binary-location convention.

    host/port are parsed from ``context.database_url`` -- the SAME endpoint
    the rest of the engine's seams already connect to via SQLAlchemy -- so a
    non-default port/host configuration is honored; unparsable/absent parts
    fall back to the D4 provisioning CLI's own defaults (127.0.0.1:5432).
    """

    from civiccast.native.provision.__main__ import resolve_provision_paths

    paths = resolve_provision_paths(install_root=context.install_root)
    pg_ctl_path = str(Path(paths.initdb_path).with_name("pg_ctl.exe"))

    host = "127.0.0.1"
    port = 5432
    # An unparsable database_url falls back to the D4 defaults silently here
    # -- it is diagnosed loudly elsewhere (schema_revision/backup/migrate all
    # connect with the same URL and will fail there with the real cause).
    with contextlib.suppress(Exception):
        from sqlalchemy.engine import make_url

        # normalize_database_url is a no-op for host/port parsing purposes
        # (it only rewrites the driver name), but applying it here keeps
        # every make_url/create_engine call in this module consistent, and
        # matches the SAME url the actual connect attempt below will use.
        url = make_url(normalize_database_url(context.database_url))
        host = url.host or host
        port = url.port or port

    return PgLifecyclePaths(
        pg_ctl_path=pg_ctl_path,
        data_dir=paths.postgres_data_dir,
        host=host,
        port=port,
    )


def _pg_ctl_spec(paths: PgLifecyclePaths) -> ChildSpec:
    return postgres_child_spec(
        pg_ctl_path=paths.pg_ctl_path,
        data_dir=paths.data_dir,
        host=paths.host,
        port=paths.port,
    )


# ---------------------------------------------------------------------------
# Real primitives (lazy subprocess/SQLAlchemy/SCM; only fire when invoked)
# ---------------------------------------------------------------------------


#: psycopg's own courtesy hint to the driver -- kept unchanged from before
#: this fix. NOT what actually bounds this function's wait (see
#: ``_REACHABLE_HARD_WAIT_CEILING_SECONDS`` below).
_REACHABLE_CONNECT_TIMEOUT_SECONDS = 5

#: BLOCKER #52 audit finding (file:line references are to this module and
#: tests/native/test_upgrade_pg_lifecycle.py as they stood before this fix):
#:
#: ``connect_args={"connect_timeout": 5}`` was ALREADY present on the single
#: connect attempt below, yet
#: ``tests/native/test_upgrade_pg_lifecycle.py::
#: test_real_database_reachable_false_on_connection_error`` -- a test that
#: calls this exact function against a real (refused-connection) SQLAlchemy
#: engine -- carries this DISCLOSED, already-measured finding in the very
#: next test's docstring (test_real_database_reachable_normalizes_bare_
#: postgresql_scheme, ~test_upgrade_pg_lifecycle.py:283-286 before this
#: change): "times out ~130s on this box's psycopg v3 against a refused
#: connection ... that duration is a reported, not silently absorbed,
#: finding". In other words: passing ``connect_timeout`` to psycopg v3 does
#: NOT reliably bound the wait on this platform -- it is a hint the driver
#: is observed not to honor for every connect failure mode, not a hard
#: ceiling. This module previously had NO enforcement independent of that
#: hint, so a stalled connect attempt here was genuinely unbounded from this
#: function's own point of view -- exactly the mechanism BLOCKER #52's
#: Sandbox run 14 (25+ minute hang, engine CPU ~2.6s -- almost entirely
#: blocked in I/O, not looping) points at: the D3 engine's very first
#: DB-touching seam blocking far longer than its 5s "budget" implied.
#:
#: The fix: this function no longer trusts connect_timeout as the bound. It
#: runs the connect attempt on a worker thread and enforces THIS ceiling via
#: ``Future.result(timeout=...)`` itself -- a hard wall-clock deadline this
#: function's own caller can never wait past, regardless of what psycopg/the
#: OS TCP stack does with the underlying socket. A thread that is still
#: blocked when the ceiling is reached is abandoned (its eventual result is
#: discarded; nothing waits on it) rather than joined, which is what makes
#: the bound actually hold -- joining an abandoned thread would just
#: reintroduce the same unbounded wait one level up.
_REACHABLE_HARD_WAIT_CEILING_SECONDS = 10.0


#: Task #55: the classification itself now lives in
#: :func:`civiccast.db.guarded_connect.classify_missing_database` (shared
#: with :func:`civiccast.schema_check.read_db_revision`) -- this module-level
#: name is kept as a plain alias so existing imports/call sites in this
#: module and ``tests/native/test_upgrade_pg_lifecycle.py`` (which imports
#: ``pg_lifecycle._is_missing_database_error`` directly) keep working
#: unmodified. See the shared function's docstring for the full rationale.
_is_missing_database_error = classify_missing_database


def _probe_database_once(database_url: str) -> bool:
    """One connect + ``SELECT 1`` attempt. Returns ``True``/``False`` for an
    ordinary reachable/unreachable outcome; raises :class:`DatabaseMissingError`
    immediately (never swallowed) when the target database itself does not
    exist -- see that class's docstring for why this is not ordinary
    unreachability. Runs on the caller's thread; :func:`real_database_reachable`
    is what bounds the wait."""

    from sqlalchemy import create_engine
    from sqlalchemy.exc import DBAPIError

    # normalize_database_url: a bare `postgresql://` scheme maps to the
    # uninstalled psycopg2 dialect (ADR 0008 ships psycopg v3 only) -- beta
    # BLOCKER #51.
    engine = create_engine(
        normalize_database_url(database_url),
        poolclass=None,
        # The 5 s reachability bound is site-local and load-bearing (BLOCKER
        # #51 upgrade-loop timing); the explicit override keeps it from
        # drifting with CIVICCAST_DB_CONNECT_TIMEOUT global tuning.
        **connect_options(database_url, timeout_seconds=_REACHABLE_CONNECT_TIMEOUT_SECONDS),
    )
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True
    except DBAPIError as exc:
        if _is_missing_database_error(exc):
            raise DatabaseMissingError(
                "PostgreSQL is reachable, but the target database named in "
                "DatabaseUrl does not exist (SQLSTATE 3D000 / "
                "InvalidCatalogName). D4 provisioning never created it "
                "(BLOCKER #52) -- this is not a startup race: starting "
                "postgres or retrying cannot fix it. Re-run native "
                "provisioning (civiccast.native.provision) against this "
                "cluster to create the database, then retry."
            ) from exc
        return False
    except Exception:
        # Every other connect failure (refused, blackholed, auth failure,
        # ...) is ordinary unreachability -- never propagated, matching this
        # module's pre-existing "never raises" contract for that case.
        return False
    finally:
        engine.dispose()


def real_database_reachable(database_url: str) -> bool:
    """Cheap DB liveness probe over a short-lived SQLAlchemy connection.

    Never raises for ordinary unreachability; DOES raise
    :class:`DatabaseMissingError` immediately when postgres is up but the
    target database is missing (BLOCKER #52 -- see that class's docstring).
    Bounded by :data:`_REACHABLE_HARD_WAIT_CEILING_SECONDS` regardless of
    what the underlying driver/OS does with a stalled connect -- see that
    constant's docstring for the measured, disclosed gap this closes.
    """

    # Task #55: the hard-ceiling enforcement itself (the ThreadPoolExecutor +
    # abandon-on-timeout mechanics) now lives in
    # civiccast.db.guarded_connect.run_bounded, shared with
    # schema_check.read_db_revision -- see that function's docstring for why
    # abandoning (never joining) a still-blocked worker is load-bearing.
    try:
        return run_bounded(
            lambda: _probe_database_once(database_url),
            _REACHABLE_HARD_WAIT_CEILING_SECONDS,
        )
    except concurrent.futures.TimeoutError:
        # The connect attempt did not return within our own hard ceiling --
        # treat exactly like an ordinary unreachable outcome (never raises
        # here; a genuinely missing database is detected INSIDE the worker,
        # which normally returns well within this ceiling since postgres
        # itself answers FATAL almost immediately -- see DatabaseMissingError).
        return False


def real_service_registered_probe() -> bool | None:
    """Is the CivicCastSupervisor service registered in the SCM at all --
    reuses :func:`civiccast.native.upgrade.service_control.
    _real_service_registered_probe` verbatim (the same "is it provably
    absent" question the drain seam's upgrade-routing fix already
    answers), not a second SCM-query call-site."""

    from civiccast.native.upgrade.service_control import _real_service_registered_probe

    return _real_service_registered_probe()


def real_start_postgres(paths: PgLifecyclePaths) -> None:
    """``pg_ctl start -D <data_dir> -w -o "-p <port> -h <host>"`` -- the
    exact argv :func:`~civiccast.native.supervisor.children.postgres_child_spec`
    builds (the one blessed pg_ctl call-site), run via ``subprocess.run``.
    ``-w`` makes pg_ctl itself block until the server is accepting
    connections (or its own wait budget elapses), so no separate readiness
    poll is layered on top here.

    Row-4b (Sandbox run 21): the data directory's ACL is normalized FIRST --
    see :mod:`civiccast.native.pgdata_acl` for the full mechanism. This is
    the exact call that died with ``FATAL: could not open file
    "pg_wal/000000010000000000000002": Permission denied`` on the documented
    update path, because ``pg_ctl`` launches the postmaster under a
    restricted token in which ``BUILTIN\\Administrators`` is deny-only, and
    the WAL segments were created by the LocalSystem service. Normalizing
    here (rather than only at provisioning time) is what repairs a cluster
    that a PREVIOUS install already left in that state -- provisioning has
    not run yet at this point in the NSIS chain, and on this path it may be
    a different administrator account running the reinstall. A normalization
    failure is fail-loud and no ``pg_ctl`` is attempted."""

    try:
        normalize_pgdata_acl(paths.data_dir)
    except PgDataAclError as exc:
        raise PostgresLifecycleError(
            f"pg_ctl start was not attempted (data_dir={paths.data_dir!r}): {exc}"
        ) from exc

    spec = _pg_ctl_spec(paths)
    try:
        # FILE-backed capture (pg_ctl_exec) -- capture_output pipes here were
        # run 14's ">25-minute idle engine": the spawned postgres inherited
        # the pipe handles and this call never returned; the timeout did not
        # save it (CPython re-drains the pipes after killing pg_ctl).
        result = run_pg_ctl_argv(spec.argv, timeout_seconds=_START_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PostgresLifecycleError(
            "pg_ctl start could not be run (data_dir="
            f"{paths.data_dir!r} host={paths.host!r} port={paths.port}): {exc}"
        ) from exc
    if result.returncode != 0:
        raise PostgresLifecycleError(
            f"pg_ctl start failed (exit {result.returncode}, data_dir="
            f"{paths.data_dir!r} host={paths.host!r} port={paths.port}): "
            f"{_tail(result.output_tail)}"
        )


def real_stop_postgres(paths: PgLifecyclePaths) -> None:
    """``pg_ctl stop -D <data_dir> -m fast`` -- the spec-pinned graceful
    stop text, resolved via
    :func:`~civiccast.native.supervisor.children.graceful_stop_action`
    (postgres's stop argv needs no pid substitution, so the ``pid`` argument
    here is inert)."""

    spec = _pg_ctl_spec(paths)
    action = graceful_stop_action(spec, pid=0)
    argv = action.argv
    assert argv is not None  # postgres's graceful stop is always "argv" kind
    try:
        # Same file-backed capture as start; stop cannot spawn a lingering
        # server but the belt costs nothing and keeps one execution path.
        result = run_pg_ctl_argv(argv, timeout_seconds=_STOP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PostgresLifecycleError(
            f"pg_ctl stop could not be run (data_dir={paths.data_dir!r}): {exc}"
        ) from exc
    if result.returncode != 0:
        raise PostgresLifecycleError(
            f"pg_ctl stop failed (exit {result.returncode}, data_dir="
            f"{paths.data_dir!r}): {_tail(result.output_tail)}"
        )


# ---------------------------------------------------------------------------
# Pure decision logic (fully unit-tested; no Windows/Postgres/subprocess)
# ---------------------------------------------------------------------------


def wrap_schema_revision(
    schema_revision: SchemaRevisionFn,
    *,
    database_reachable: DatabaseReachableFn,
    service_registered_probe: ServiceRegisteredProbe,
    start_postgres: StartPostgresFn,
    state: PgLifecycleState,
) -> SchemaRevisionFn:
    """Wrap ``schema_revision`` -- the D3 engine's FIRST DB-touching seam --
    with the BLOCKER #49 lifecycle decision, made at most ONCE per run
    (``state.checked`` guards the second, post-migration call).

    * reachable                              -> no-op; ``state.started_by_us``
      stays False, so the caller's stop-in-finally never fires.
    * unreachable, scoped ownership allowed (probe returns exactly ``False``) ->
      ``start_postgres()``; ``state.started_by_us = True``.
    * unreachable, scoped ownership unsafe/ambiguous (anything but ``False``) ->
      raise :class:`PostgresLifecycleError` (``FAIL_CLOSED_DETAIL``),
      fail-closed, unchanged behavior otherwise.

    A ``start_postgres`` failure raises BEFORE ``schema_revision`` is ever
    called and before ``state.started_by_us`` is set -- no engine work is
    attempted, and the caller's finally has nothing to stop.
    """

    def _call() -> str | None:
        if not state.checked:
            state.checked = True
            if not database_reachable():
                if service_registered_probe() is not False:
                    raise PostgresLifecycleError(FAIL_CLOSED_DETAIL)
                start_postgres()
                state.started_by_us = True
        return schema_revision()

    return _call


def service_allows_scoped_postgres_start(*, registered: bool | None, stopped: bool | None) -> bool:
    """Whether this installer process can safely become postgres's owner.

    An absent service is the existing uninstall/reinstall case. A registered
    but definitively STOPPED service is the install-over-existing case: keeping
    registration preserves D3's product identity, while STOPPED proves it has
    no process tree that can own the cluster. Every ambiguous or live state
    fails closed.
    """

    return registered is False or stopped is True


def real_scoped_postgres_start_allowed() -> bool:
    registered = real_service_registered_probe()
    if registered is False:
        return True

    from civiccast.native.upgrade.service_control import _real_service_stopped_probe

    return service_allows_scoped_postgres_start(
        registered=registered,
        stopped=_real_service_stopped_probe(),
    )


def wrap_health_gate(
    health_gate: HealthGateFn,
    *,
    stop_postgres: StopPostgresFn,
    state: PgLifecycleState,
) -> HealthGateFn:
    """Hand a scoped postgres process back to the supervisor before health.

    The health gate starts the registered service in maintenance mode. Its
    supervisor must be the sole postgres owner, so a pg_ctl process started by
    this D3 run is stopped first. The state latch is cleared only after that
    stop succeeds; the outer ``finally`` therefore retries a failed stop.
    """

    def _call() -> bool:
        if state.started_by_us:
            stop_postgres()
            state.started_by_us = False
        return health_gate()

    return _call


def wrap_restore_backup(
    restore_backup: RestoreBackupFn,
    *,
    database_reachable: DatabaseReachableFn,
    scoped_start_allowed: Callable[[], bool],
    start_postgres: StartPostgresFn,
    state: PgLifecycleState,
) -> RestoreBackupFn:
    """Ensure rollback restore has a safely-owned PostgreSQL process.

    A failed service handoff can leave the service stopped after the scoped
    process was shut down for health. Rollback still owes the verified restore;
    restart postgres only when the database is unreachable and the SCM proves
    this installer may own it.
    """

    def _call(backup: BackupRef) -> None:
        if not database_reachable():
            if not scoped_start_allowed():
                raise PostgresLifecycleError(FAIL_CLOSED_DETAIL)
            start_postgres()
            state.started_by_us = True
        restore_backup(backup)

    return _call


def attach_pg_lifecycle(
    seams: UpgradeSeams,
    context: UpgradeContext,
) -> tuple[UpgradeSeams, StopPostgresFn]:
    """Return ``(lifecycle-wrapped-seams, stop_if_started)``.

    ``stop_if_started`` MUST be called in a ``finally`` around the engine
    run (:func:`civiccast.native.upgrade.orchestrator.run_upgrade`) -- it
    issues the real ``pg_ctl stop -m fast`` iff THIS run started postgres,
    so a postgres this process brought up is never left running for D4
    provisioning to trip over, and a postgres this process did NOT start
    (already reachable, or owned by the running service) is never touched.

    Deriving paths and wrapping the seams is inert (no subprocess/SQL/SCM
    call fires here); the real primitives only run when a wrapped seam is
    actually invoked by the orchestrator, so a
    refusal (REFUSED_NON_RESTORABLE) or terminal-journal no-op -- neither of
    which ever call ``schema_revision`` -- never touches postgres.
    """

    paths = derive_pg_lifecycle_paths(context)
    state = PgLifecycleState()

    def database_reachable() -> bool:
        return real_database_reachable(context.database_url)

    def start_postgres() -> None:
        real_start_postgres(paths)

    def stop_postgres() -> None:
        real_stop_postgres(paths)

    scoped_start_allowed = real_scoped_postgres_start_allowed

    def ensure_postgres() -> None:
        if database_reachable():
            return
        if not scoped_start_allowed():
            raise PostgresLifecycleError(FAIL_CLOSED_DETAIL)
        start_postgres()
        state.started_by_us = True

    def wrapped_drain() -> bool:
        ensure_postgres()
        return seams.drain_and_verify_quiescence()

    def wrapped_backup(backup_dir: str) -> BackupRef:
        ensure_postgres()
        return seams.backup(backup_dir)

    def wrapped_migrate() -> None:
        ensure_postgres()
        seams.migrate()

    wrapped_schema_revision = wrap_schema_revision(
        seams.schema_revision,
        database_reachable=database_reachable,
        service_registered_probe=lambda: False if scoped_start_allowed() else None,
        start_postgres=start_postgres,
        state=state,
    )
    wrapped_health_gate = wrap_health_gate(
        seams.health_gate,
        stop_postgres=stop_postgres,
        state=state,
    )
    wrapped_restore_backup = wrap_restore_backup(
        seams.restore_backup,
        database_reachable=database_reachable,
        scoped_start_allowed=scoped_start_allowed,
        start_postgres=start_postgres,
        state=state,
    )
    new_seams = dataclasses.replace(
        seams,
        schema_revision=wrapped_schema_revision,
        drain_and_verify_quiescence=wrapped_drain,
        backup=wrapped_backup,
        migrate=wrapped_migrate,
        health_gate=wrapped_health_gate,
        restore_backup=wrapped_restore_backup,
    )

    def _stop_if_started() -> None:
        if state.started_by_us:
            stop_postgres()

    return new_seams, _stop_if_started


__all__ = [
    "FAIL_CLOSED_DETAIL",
    "DatabaseMissingError",
    "PgLifecyclePaths",
    "PgLifecycleState",
    "PostgresLifecycleError",
    "attach_pg_lifecycle",
    "derive_pg_lifecycle_paths",
    "real_database_reachable",
    "real_scoped_postgres_start_allowed",
    "real_service_registered_probe",
    "real_start_postgres",
    "real_stop_postgres",
    "service_allows_scoped_postgres_start",
    "wrap_health_gate",
    "wrap_restore_backup",
    "wrap_schema_revision",
]
