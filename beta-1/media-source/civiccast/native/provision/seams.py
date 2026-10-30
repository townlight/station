# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Default (real) seam implementations for the provisioning engine.

The orchestrator is pure over
:class:`~civiccast.native.provision.models.ProvisionSeams`; this module
builds the PRODUCTION bundle, mirroring
:mod:`civiccast.native.upgrade.seams`'s structure:

* ``verify_pack``             -> :func:`civiccast.native.provision.pack.verify_server_binaries_pack`
* ``detect_postgres_cluster`` -> read ``<data_dir>/PG_VERSION``
* ``run_initdb``              -> subprocess ``initdb`` (argv built by
                                  :func:`initdb_argv`, kept separate from the
                                  spawn so the shape is unit-testable without
                                  a real process)
* ``write_postgres_conf`` / ``write_pg_hba_conf`` ->
  atomic file writes (:func:`_atomic_write_text`)
* ``ensure_database``         -> BLOCKER #52: ``pg_ctl start`` the
                                  just-configured cluster, ``CREATE
                                  DATABASE`` over ``psql`` if absent,
                                  ``pg_ctl stop`` in a ``finally``
                                  (:func:`default_ensure_database`)

NATS JetStream was removed from the product entirely (owner decision
2026-08-20, ADR 0023 "NATS removed -- in-process event bus", which supersedes
ADR 0001); this module no longer wires a NATS store-directory or config-file
seam.

None of the real subprocess/filesystem actions this module wires are
exercised against a real PostgreSQL binary in the unit suite (WS5 task's
hard rule: "NO real postgres execution in unit tests" -- that proof belongs
to the WP2/WP5 live lifecycle matrix). :func:`initdb_argv`'s pure argv-shape
and the atomic-write helper ARE unit-tested, since neither spawns a process
or requires PostgreSQL to exist on the test host.

PROVISION NOTE (encoding): :func:`initdb_argv` pins ``--encoding=UTF8
--locale=C`` for every NEW cluster (``run_initdb`` only ever fires when
``detect_postgres_cluster`` finds no existing ``PG_VERSION``, see
``orchestrator.py``'s ``needs_initdb`` branch). A cluster that already
existed before this change is NEVER touched here and keeps whatever
encoding it was originally initdb'd with -- on Windows that was previously
the OS codepage (WIN1252 in the sandbox/CI fleet), since nothing passed
``-E``/``--encoding``/``--locale`` before. Any code path that persists
operator-entered or child-process free text must therefore still assume the
database MAY be non-UTF8 and go through ``civiccast/egress/_text.py``'s
``db_safe_text`` -- it must never assume this fix's UTF8 pin applies
cluster-wide.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from civiccast.native.pg_ctl_exec import CapturedProcess, run_captured_argv, run_pg_ctl_argv
from civiccast.native.pgdata_acl import _current_process_sid, normalize_pgdata_acl
from civiccast.native.provision.conf import render_pg_hba_conf, render_pg_hba_trust_conf
from civiccast.native.provision.models import (
    DatabaseDecision,
    ProvisionContext,
    ProvisionSeams,
)
from civiccast.native.provision.pack import verify_server_binaries_pack

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from civiccast.native.provision.models import ProvisionPlan


def _atomic_write_text(path: str | Path, content: str) -> None:
    """Write ``content`` to ``path`` via temp-file + ``os.replace`` (same
    atomicity contract as the provisioning journal itself -- a kill mid-write
    never leaves a torn config file)."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(target)


def default_detect_postgres_cluster(context: ProvisionContext) -> Callable[[], str | None]:
    def _detect() -> str | None:
        pg_version_path = Path(context.postgres_data_dir) / "PG_VERSION"
        try:
            return pg_version_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None

    return _detect


# ---------------------------------------------------------------------------
# C2 (2026-07-31): hard deadlines for every install-time subprocess this
# module spawns. None of these previously carried a timeout at all, so one
# hung child stalled the whole NSIS install chain forever; and the repo has
# live-proven (Sandbox runs 14/15) that on Windows ``capture_output=True``
# plus a child that spawns lingering descendants blocks past ANY ``timeout=``
# -- so ``initdb``/``psql`` (both spawn/contact server-side processes) route
# through :func:`civiccast.native.pg_ctl_exec.run_captured_argv`'s
# file-backed capture, never pipes. ``icacls`` spawns no descendants; a
# plain hard timeout on its existing call shape suffices.
# ---------------------------------------------------------------------------

#: icacls on one small local file is near-instant; a minute is generous.
ICACLS_TIMEOUT_SECONDS = 60.0
#: initdb writes a whole fresh cluster -- generous for a slow municipal
#: disk, but still a HARD stop well inside the installer's own patience.
INITDB_TIMEOUT_SECONDS = 300.0
#: Each psql call is one statement against a fresh local server.
PSQL_TIMEOUT_SECONDS = 120.0


#: The auth method pinned for TCP/IP ("host") connections at ``initdb`` time.
#: MUST stay textually identical to the method
#: :func:`civiccast.native.provision.conf.render_pg_hba_conf` writes for its
#: one loopback rule -- see :func:`initdb_argv`'s docstring for why.
INITDB_AUTH_HOST_METHOD = "scram-sha-256"

#: Server encoding pinned for every NEW cluster -- see :func:`initdb_argv`'s
#: docstring. Existing clusters (initdb'd before this constant existed) are
#: untouched and may still be the OS codepage (WIN1252 on Windows).
INITDB_ENCODING = "UTF8"

#: Locale pinned for every NEW cluster -- see :func:`initdb_argv`'s docstring
#: for why ``C`` (byte-ordering, always available, no locale pack needed)
#: rather than a language-specific locale.
INITDB_LOCALE = "C"


def initdb_argv(*, initdb_path: str, data_dir: str, username: str, pwfile: str) -> list[str]:
    """Pure argv construction for ``initdb`` -- separated from the spawn
    itself so the exact command shape is unit-testable without running a
    real ``initdb`` binary.

    ``--pwfile`` supplies the bootstrap superuser's password so the cluster
    ``initdb`` creates actually has a password set (without it, ``initdb``
    creates the role with NO password at all, and every subsequent
    connection using the ``DatabaseUrl`` this engine resolves would fail --
    the exact bug this function was written to close). ``pwfile`` is always
    a path to a short-lived, owner-restricted temp file
    (:func:`_initdb_pwfile`); the password itself never appears in argv.

    ``--auth-host=<method>`` (:data:`INITDB_AUTH_HOST_METHOD`) is pinned
    rather than the umbrella ``--auth=``/``-A`` flag deliberately: PostgreSQL
    on Windows has
    no Unix-domain sockets, so ``initdb`` never emits a ``local`` line into
    its own (immediately-overwritten) generated ``pg_hba.conf`` -- only the
    ``host`` auth default is ever meaningful on this platform. Pinning
    ``--auth-host`` specifically (instead of ``--auth``, which sets both
    ``--auth-local`` and ``--auth-host`` to the same value) names exactly the
    one setting that matters here and keeps it textually tied to
    :func:`civiccast.native.provision.conf.render_pg_hba_conf`'s ``host``
    rule via the shared :data:`INITDB_AUTH_HOST_METHOD` constant, so the two
    can never silently drift apart (a freshly initialized cluster's default
    auth is never weaker -- or different -- from what the provisioned
    ``pg_hba.conf`` will require once the config-write phase runs).

    ``--encoding=UTF8 --locale=C`` (:data:`INITDB_ENCODING`,
    :data:`INITDB_LOCALE`) pin the cluster's server encoding rather than
    inheriting ``initdb``'s own platform default. On Windows that default is
    the process's OS codepage (WIN1252 in the sandbox/CI fleet) because
    nothing here ever passed ``-E``/``--encoding``/``--locale`` -- and no
    caller sets ``client_encoding`` either (:mod:`civiccast.db.session`,
    :mod:`civiccast.db.url`) -- so any character outside that codepage
    written into a persisted free-text column (an operator-entered source
    title, a folded child-stderr tail, ...) raised ``UnicodeEncodeError`` in
    psycopg and aborted the whole automation pass for that channel (see
    ``civiccast/egress/_text.py``'s ``db_safe_text`` and
    ``civiccast/egress/daemon.py``'s ``_ascii_safe`` docstring for the T6
    soak evidence). UTF8 can encode any Unicode text, closing the defect at
    its root for every NEW cluster. ``C`` locale (rather than a language-
    specific one) is deliberate too: it is byte-ordering, not a translated
    collation, so it is available on every platform without a locale pack
    and never varies by OS install language -- ``LC_COLLATE``/``LC_CTYPE``
    ordering only needs to be self-consistent within one cluster, not match
    any particular human language.

    This only ever changes what a NEW cluster gets: ``initdb`` is only
    invoked by the orchestrator when ``detect_postgres_cluster`` finds no
    existing ``PG_VERSION`` file (see ``orchestrator.py``'s
    ``needs_initdb`` branch) -- an existing cluster provisioned before this
    change keeps whatever encoding it already has, and nothing here
    retroactively touches it. That is exactly why the encoding cannot be
    assumed UTF8 anywhere a persisted value might reach the database: see
    ``civiccast/egress/_text.py``.
    """

    if not initdb_path.strip():
        raise ValueError("initdb_path must not be empty")
    if not data_dir.strip():
        raise ValueError("data_dir must not be empty")
    if not username.strip():
        raise ValueError("username must not be empty")
    if not pwfile.strip():
        raise ValueError("pwfile must not be empty")
    return [
        initdb_path,
        "--pgdata",
        data_dir,
        "--username",
        username,
        "--pwfile",
        pwfile,
        f"--auth-host={INITDB_AUTH_HOST_METHOD}",
        "--encoding",
        INITDB_ENCODING,
        "--locale",
        INITDB_LOCALE,
        "--no-instructions",
    ]


def _windows_pwfile_acl_command(path: Path, *, caller_sid: str) -> list[str]:
    """Return the ``icacls`` command used to remove inherited access from a
    just-written initdb superuser password file.

    Mirrors :func:`civiccast.certs.authority._windows_private_key_acl_command`'s
    contract for the same class of credential-bearing temp file (grant only
    the current user, SYSTEM, and Administrators; strip inheritance).
    """

    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", caller_sid):
        raise RuntimeError("Cannot restrict initdb password-file ACL: invalid Windows token SID.")
    return [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"*{caller_sid}:F",
        "*S-1-5-18:F",
        "*S-1-5-32-544:F",
    ]


def _restrict_windows_pwfile_acl(path: Path) -> None:
    """Restrict the initdb password file's ACL on Windows. Exposed as its
    own (non-branching) function -- rather than folded into
    :func:`_initdb_pwfile` -- so it is directly unit-testable on any host OS
    (mirrors :func:`civiccast.certs.authority._restrict_windows_private_key_acl`)."""

    # The token user survives PostgreSQL's restricted child token; USERNAME
    # may instead name an unresolvable machine account under LocalSystem.
    caller_sid = _current_process_sid()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(  # noqa: S603 - fixed icacls argv; no shell or user-built command line.
        _windows_pwfile_acl_command(path, caller_sid=caller_sid),
        check=True,
        capture_output=True,
        text=True,
        timeout=ICACLS_TIMEOUT_SECONDS,
        creationflags=creationflags,
    )


@contextmanager
def _initdb_pwfile(work_root: str | Path, password: str) -> Iterator[Path]:
    """Create an owner-restricted temp file containing exactly
    ``f"{password}\\n"``, yield its path, and unconditionally delete it
    (success, ``initdb`` failure, or an exception raised inside the
    ``with`` block) so the plaintext superuser password never outlives the
    single ``initdb`` invocation that needs it.

    Lives under ``work_root`` / ``"tmp"`` -- ``work_root`` is always the
    provisioning run's own ``state_root`` (ProgramData, the same
    already-established provisioning work root
    :class:`~civiccast.native.provision.models.ProvisionContext`'s docstring
    names as ACL-scoped, not world-readable) -- NEVER the system temp
    directory, which is commonly world-readable/world-writable and outlives
    this process's own lifetime guarantees.
    """

    work_dir = Path(work_root) / "tmp"
    work_dir.mkdir(parents=True, exist_ok=True)
    pwfile = work_dir / f"initdb-pwfile-{os.getpid()}-{secrets.token_hex(8)}.tmp"
    pwfile.write_text(f"{password}\n", encoding="utf-8")
    try:
        if os.name == "nt":
            _restrict_windows_pwfile_acl(pwfile)
        else:
            pwfile.chmod(0o600)
        yield pwfile
    finally:
        pwfile.unlink(missing_ok=True)


def _decode_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


def _decode_both_streams(result: CapturedProcess) -> str:
    """Concatenate stdout+stderr rather than preferring stderr.

    Same precedence-bug class as ``pg_ctl_exec.run_pg_ctl_argv`` (see that
    module's docstring for the forensically-proven Windows stream-merge
    mechanism): picking only ``stderr or stdout`` can silently discard the
    real diagnostic text when the tool being run (initdb/psql) wrote its
    useful detail to the OTHER stream. Concatenating both never discards
    either.
    """

    return "\n".join(
        part for part in (_decode_output(result.stdout), _decode_output(result.stderr)) if part
    )


def default_run_initdb(
    context: ProvisionContext, *, initdb_path: str, database_username: str
) -> Callable[[], None]:
    def _run() -> None:
        Path(context.postgres_data_dir).mkdir(parents=True, exist_ok=True)
        with _initdb_pwfile(context.state_root, context.database_password) as pwfile:
            argv = initdb_argv(
                initdb_path=initdb_path,
                data_dir=context.postgres_data_dir,
                username=database_username,
                pwfile=str(pwfile),
            )
            # C2: file-backed capture + hard deadline + kill-tree on expiry
            # (initdb spawns server-side helper children on Windows) -- see
            # the timeout-constants block above.
            result = run_captured_argv(argv, timeout_seconds=INITDB_TIMEOUT_SECONDS)
            if result.returncode != 0:
                raise RuntimeError(
                    f"initdb failed (exit {result.returncode}): {_decode_both_streams(result)}"
                )

    return _run


def default_write_postgres_conf(context: ProvisionContext) -> Callable[[str], None]:
    def _write(content: str) -> None:
        _atomic_write_text(context.postgres_config_path, content)

    return _write


def default_write_pg_hba_conf(context: ProvisionContext) -> Callable[[str], None]:
    def _write(content: str) -> None:
        _atomic_write_text(context.postgres_hba_path, content)

    return _write


#: The maintenance database every PostgreSQL cluster always has -- CREATE
#: DATABASE / a pg_database lookup must run against a DIFFERENT database than
#: the one being created, and "postgres" is the one initdb always creates.
_MAINTENANCE_DATABASE = "postgres"


def _pg_ctl_argv_start(pg_ctl_path: str, context: ProvisionContext) -> list[str]:
    return [
        pg_ctl_path,
        "start",
        "-D",
        context.postgres_data_dir,
        "-w",
        "-o",
        f"-p {context.postgres_port} -h {context.postgres_host}",
    ]


def _pg_ctl_argv_stop(pg_ctl_path: str, context: ProvisionContext) -> list[str]:
    # Matches civiccast.native.supervisor.children.postgres_child_spec's
    # graceful-stop text verbatim (the one spec-fixed fact, r2-children):
    # "pg_ctl stop -m fast".
    return [pg_ctl_path, "stop", "-D", context.postgres_data_dir, "-m", "fast"]


def _psql_argv_base(psql_path: str, context: ProvisionContext, plan: ProvisionPlan) -> list[str]:
    return [
        psql_path,
        "--host",
        context.postgres_host,
        "--port",
        str(context.postgres_port),
        "--username",
        plan.database_username,
        "--no-password",
        "--dbname",
        _MAINTENANCE_DATABASE,
    ]


def _psql_env(context: ProvisionContext) -> dict[str, str]:
    # Mirrors civiccast.dr.backup.create_fresh_postgres_database's
    # PGPASSWORD-via-env convention (the one existing "psql command-runner"
    # precedent in this codebase) rather than putting the password on argv,
    # where it would be visible to any process listing.
    env = dict(os.environ)
    env["PGPASSWORD"] = context.database_password
    return env


def _run_pg_ctl(argv: list[str], *, action: str) -> None:
    # FILE-backed capture via pg_ctl_exec -- NEVER pipes. Sandbox run 15
    # burned a 45-minute fresh-install timeout because pg_ctl start's
    # spawned postgres inherited capture_output's pipe handles and
    # subprocess.run blocked reading them forever (see pg_ctl_exec's module
    # docstring for the full mechanism).
    result = run_pg_ctl_argv(argv, timeout_seconds=180.0)
    if result.returncode != 0:
        raise RuntimeError(
            f"pg_ctl {action} failed while preparing to create the database "
            f"(exit {result.returncode}): {result.output_tail}"
        )


def _start_provisioned_cluster(pg_ctl_path: str, context: ProvisionContext) -> None:
    """Normalize the data directory's ACL, then ``pg_ctl start``.

    Row-4b (Sandbox run 21): every INSTALL-TIME ``pg_ctl start`` must be
    preceded by :func:`civiccast.native.pgdata_acl.normalize_pgdata_acl` --
    see that module for the mechanism (``pg_ctl`` runs the postmaster under a
    restricted token with ``BUILTIN\\Administrators`` deny-only, so files the
    LocalSystem service created are read-only to it). Doing it here, at
    provisioning time, is what makes a cluster born correct: the protected,
    inheritable DACL this applies is what every future WAL segment the
    service creates inherits, so the NEXT update's D3 start does not depend
    on ``C:\\ProgramData``'s ``CREATOR OWNER`` ACE naming the right account.
    It also tightens a fresh cluster (``BUILTIN\\Users`` loses the read and
    add-file access ``C:\\ProgramData`` hands down by default).

    Idempotent, so the reuse/repair paths through this seam re-assert it on
    an existing cluster rather than needing a separate remediation tool. A
    normalization failure raises
    :class:`~civiccast.native.pgdata_acl.PgDataAclError` (a ``RuntimeError``,
    handled exactly like every other fail-loud provisioning seam fault) and
    no ``pg_ctl`` is attempted.
    """

    normalize_pgdata_acl(context.postgres_data_dir)
    _run_pg_ctl(_pg_ctl_argv_start(pg_ctl_path, context), action="start")


def _database_exists(psql_path: str, context: ProvisionContext, plan: ProvisionPlan) -> bool:
    """Query ``pg_database`` for ``plan.database_name`` over ``psql``.

    ``plan.database_name``/``plan.database_username`` are both validated as
    safe SQL identifiers at :class:`~civiccast.native.provision.models.
    ProvisionPlan` construction time (``_IDENTIFIER_RE``), so the literal
    embedded in this query is never attacker-controlled input.
    """

    argv = [
        *_psql_argv_base(psql_path, context, plan),
        "--tuples-only",
        "--no-align",
        "-c",
        f"SELECT 1 FROM pg_database WHERE datname = '{plan.database_name}'",  # noqa: S608  # nosec B608 - identifier validated by _IDENTIFIER_RE
    ]
    # C2: file-backed capture + hard deadline (psql contacts the postgres
    # server process family; capture_output pipes are the proven hang).
    result = run_captured_argv(argv, timeout_seconds=PSQL_TIMEOUT_SECONDS, env=_psql_env(context))
    if result.returncode != 0:
        raise RuntimeError(
            f"psql failed while checking whether database {plan.database_name!r} exists "
            f"(exit {result.returncode}): "
            f"{_decode_both_streams(result)}"
        )
    return _decode_output(result.stdout) == "1"


def _create_database(psql_path: str, context: ProvisionContext, plan: ProvisionPlan) -> None:
    """``CREATE DATABASE "<name>" OWNER "<username>"`` over ``psql``.

    ``OWNER`` cannot be parameterized (DDL takes no bind parameters), which
    is exactly why ``database_name``/``database_username`` are validated as
    safe identifiers at the model layer rather than escaped here.
    """

    argv = [
        *_psql_argv_base(psql_path, context, plan),
        "--set",
        "ON_ERROR_STOP=1",
        "-c",
        f'CREATE DATABASE "{plan.database_name}" OWNER "{plan.database_username}"',
    ]
    # C2: file-backed capture + hard deadline (same rationale as
    # _database_exists above).
    result = run_captured_argv(argv, timeout_seconds=PSQL_TIMEOUT_SECONDS, env=_psql_env(context))
    if result.returncode != 0:
        raise RuntimeError(
            f"psql failed while creating database {plan.database_name!r} "
            f"(exit {result.returncode}): "
            f"{_decode_both_streams(result)}"
        )


def default_ensure_database(
    context: ProvisionContext,
    plan: ProvisionPlan,
    *,
    pg_ctl_path: str,
    psql_path: str,
) -> Callable[[], DatabaseDecision]:
    """BLOCKER #52: D4 provisioning ran initdb, wrote config, and persisted a
    DatabaseUrl naming ``plan.database_name`` -- but never executed CREATE
    DATABASE, so the installed service's own connection faulted with
    ``FATAL: database "civiccast" does not exist`` (live-proven, Sandbox run
    14; ``psycopg.errors.InvalidCatalogName`` / SQLSTATE ``3D000`` is the
    exact error the D3 upgrade engine now grounds on for the same defect,
    see :mod:`civiccast.native.upgrade.pg_lifecycle`).

    Whether the database exists can only be answered by a LIVE connection,
    and nothing before this provisioning step ever starts postgres (the
    supervisor service starts it later, at D6/D3 runtime) -- so this single
    seam owns the whole start/check/create/stop sequence: ``pg_ctl start``
    the just-configured cluster (idempotent per :func:`_run_pg_ctl`'s
    contract), query ``pg_database`` for ``plan.database_name``, run
    ``CREATE DATABASE ... OWNER <database_username>`` only if absent, then
    ``pg_ctl stop`` unconditionally in a ``finally`` -- so a postgres this
    step starts is NEVER left running for the rest of provisioning (or the
    supervisor's later real start) to trip over, mirroring
    :func:`civiccast.native.upgrade.pg_lifecycle.attach_pg_lifecycle`'s
    "never left running" contract for the analogous D3 scoped-start.

    A stop failure after a successful create still propagates (fail-loud,
    not swallowed) -- an operator needs to know postgres may still be up,
    even though the database itself was created successfully.
    """

    def _ensure() -> DatabaseDecision:
        _start_provisioned_cluster(pg_ctl_path, context)
        try:
            if _database_exists(psql_path, context, plan):
                return DatabaseDecision(
                    outcome="already_exists",
                    detail=f"database {plan.database_name!r} already existed; no action taken",
                )
            _create_database(psql_path, context, plan)
            return DatabaseDecision(
                outcome="created",
                detail=(
                    f"database {plan.database_name!r} created (owner {plan.database_username!r})"
                ),
            )
        finally:
            _run_pg_ctl(_pg_ctl_argv_stop(pg_ctl_path, context), action="stop")

    return _ensure


def pg_ctl_path_for(initdb_path: str) -> str:
    """``pg_ctl.exe`` resolved as ``initdb_path``'s SIBLING in the staged
    ``native-server-binaries`` pack bin directory -- the one established
    convention (see :func:`build_default_seams_for`'s docstring). Shared by
    the seam bundle and the post-provision schema migration
    (:func:`migrate_provisioned_schema`) so the two can never drift."""

    return str(Path(initdb_path).with_name("pg_ctl.exe"))


def psql_path_for(initdb_path: str) -> str:
    """``psql.exe`` resolved as ``initdb_path``'s SIBLING, the same
    established convention :func:`pg_ctl_path_for` uses -- shared by the seam
    bundle and the N-15 adoption credential-reset so the two never drift."""

    return str(Path(initdb_path).with_name("psql.exe"))


# ---------------------------------------------------------------------------
# N-15: adoption of a surviving, product-owned PostgreSQL cluster.
#
# Uninstall (chain M / security fix F-02) deletes the DatabaseUrl registry
# credential but deliberately PRESERVES the product data directory (station
# data). A later reinstall therefore finds an initialized cluster whose
# service-role password lives nowhere on disk and cannot be reconstructed --
# the exact "re-establish a credential for a surviving, product-owned cluster"
# unit of work native_uninstall.rs's F-02 inventory entry names as still-open.
#
# The reset re-establishes a FRESH credential ON THE SAME cluster (data
# preserved: no initdb, no CREATE/DROP DATABASE) via a brief loopback-`trust`
# maintenance window -- the standard forgotten-superuser-password recovery,
# scoped to the product's own private, loopback-only data directory. It is
# fail-closed against a FOREIGN cluster living at the same path: a cluster that
# does not present the product's own superuser role AND database is REFUSED
# (AdoptionForeignClusterError), never silently taken over.
# ---------------------------------------------------------------------------


class AdoptionForeignClusterError(RuntimeError):
    """Raised by :func:`reset_cluster_credential` when the initialized cluster
    at the product data directory is NOT provably a CivicCast-provisioned
    cluster (its bootstrap superuser role or its ``civiccast`` database is
    absent). Adoption re-establishes a credential; it must never do so on a
    cluster it cannot first prove the product itself created -- so this halts
    loud rather than taking over foreign data. A distinct type so the CLI maps
    it to an honest foreign-cluster recovery document, separate from a generic
    reset fault."""


#: The service-role password is a machine-generated ``secrets.token_urlsafe``
#: value (URL-safe base64: A-Z a-z 0-9 - _), so it can be embedded in a
#: single-quoted SQL string literal without any quote character to escape.
#: This guard is defense-in-depth: it FAILS CLOSED if a password ever contains
#: anything outside that charset rather than risk an unescaped literal.
_SQL_SAFE_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Mirrors :data:`civiccast.native.provision.models._IDENTIFIER_RE` -- the
#: same safe-SQL-identifier envelope ``ProvisionPlan`` already enforces on
#: ``database_username`` at construction. Re-checked here (defense-in-depth)
#: because the value is embedded, double-quoted, into the ALTER ROLE literal.
_SQL_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def alter_role_password_sql(*, username: str, password: str) -> str:
    """Pure builder for the ``ALTER ROLE`` statement that re-establishes the
    service role's password during adoption. Separated from the spawn so its
    exact shape is unit-testable without a real ``psql``.

    ``username`` is a validated safe identifier at the model layer
    (:class:`~civiccast.native.provision.models.ProvisionPlan`); ``password``
    is charset-guarded here (:data:`_SQL_SAFE_PASSWORD_RE`) because it is
    embedded as a single-quoted literal (a ``PASSWORD`` clause cannot take a
    bind parameter). The statement is written to an owner-restricted temp file
    and fed via ``psql -f`` -- never placed on argv -- so the fresh password
    never appears in any process's command line.
    """

    if not _SQL_SAFE_IDENTIFIER_RE.match(username):
        raise ValueError(f"username is not a safe SQL identifier: {username!r}")
    if not _SQL_SAFE_PASSWORD_RE.match(password):
        raise ValueError(
            "password contains characters outside the generated URL-safe charset; "
            "refusing to embed it in a SQL literal (fail-closed)"
        )
    return f"ALTER ROLE \"{username}\" PASSWORD '{password}';\n"


@contextmanager
def _adoption_sql_file(work_root: str | Path, sql: str) -> Iterator[Path]:
    """Owner-restricted temp ``.sql`` file carrying ``sql`` (which contains the
    fresh password), yielded for ``psql -f`` and unconditionally deleted --
    the exact same short-lived, ACL-hardened-under-``state_root`` contract as
    :func:`_initdb_pwfile`, for the same reason (the plaintext credential must
    never outlive the single ``psql`` invocation that needs it, and never live
    in a world-readable temp directory)."""

    work_dir = Path(work_root) / "tmp"
    work_dir.mkdir(parents=True, exist_ok=True)
    sql_file = work_dir / f"adopt-{os.getpid()}-{secrets.token_hex(8)}.sql"
    sql_file.write_text(sql, encoding="utf-8")
    try:
        if os.name == "nt":
            _restrict_windows_pwfile_acl(sql_file)
        else:
            sql_file.chmod(0o600)
        yield sql_file
    finally:
        sql_file.unlink(missing_ok=True)


def _psql_trust_query(
    psql_path: str, context: ProvisionContext, plan: ProvisionPlan, sql: str
) -> str:
    """Run one read-only SELECT over ``psql`` during the trust maintenance
    window and return its trimmed stdout. NO ``PGPASSWORD`` is set -- the
    transient ``trust`` pg_hba is what lets the connection succeed without the
    (unrecoverable) password. A connection/query failure raises
    :class:`AdoptionForeignClusterError`: under ``trust``, a failure to
    connect AS the product's own bootstrap role means that role is absent,
    i.e. this is not the product's cluster."""

    argv = [
        *_psql_argv_base(psql_path, context, plan),
        "--tuples-only",
        "--no-align",
        "-c",
        sql,
    ]
    result = run_captured_argv(argv, timeout_seconds=PSQL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise AdoptionForeignClusterError(
            f"could not query the cluster at {context.postgres_data_dir!r} as role "
            f"{plan.database_username!r} under the adoption trust window (psql exit "
            f"{result.returncode}); it does not present the product's own bootstrap "
            "role, so it is not a CivicCast-provisioned cluster this installer may adopt"
        )
    return _decode_output(result.stdout)


def _verify_adoptable_cluster(
    psql_path: str, context: ProvisionContext, plan: ProvisionPlan
) -> None:
    """Prove the surviving cluster is genuinely CivicCast-provisioned before
    re-establishing a credential on it: its bootstrap role must exist and be a
    superuser, AND the product database must be present. Either absent ->
    :class:`AdoptionForeignClusterError` (never adopt foreign data)."""

    role_is_super = _psql_trust_query(
        psql_path,
        context,
        plan,
        f"SELECT rolsuper FROM pg_roles WHERE rolname = '{plan.database_username}'",  # noqa: S608  # nosec B608 - identifier validated by _IDENTIFIER_RE
    )
    if role_is_super != "t":
        raise AdoptionForeignClusterError(
            f"role {plan.database_username!r} is absent or not a superuser on the cluster "
            f"at {context.postgres_data_dir!r}; refusing to adopt a cluster the product "
            "did not create (fail-closed)"
        )
    database_present = _psql_trust_query(
        psql_path,
        context,
        plan,
        f"SELECT 1 FROM pg_database WHERE datname = '{plan.database_name}'",  # noqa: S608  # nosec B608 - identifier validated by _IDENTIFIER_RE
    )
    if database_present != "1":
        raise AdoptionForeignClusterError(
            f"database {plan.database_name!r} is absent on the cluster at "
            f"{context.postgres_data_dir!r}; a fully-provisioned CivicCast cluster always "
            "has it, so this is not a cluster this installer may adopt (fail-closed)"
        )


def _reset_role_password(psql_path: str, context: ProvisionContext, plan: ProvisionPlan) -> None:
    """Re-establish ``plan.database_username``'s password to
    ``context.database_password`` (the freshly generated value) via a
    single-quoted-literal ``ALTER ROLE`` fed through ``psql -f`` from an
    owner-restricted temp file -- the password never touches argv."""

    sql = alter_role_password_sql(
        username=plan.database_username, password=context.database_password
    )
    with _adoption_sql_file(context.state_root, sql) as sql_file:
        argv = [
            *_psql_argv_base(psql_path, context, plan),
            "--set",
            "ON_ERROR_STOP=1",
            "-f",
            str(sql_file),
        ]
        result = run_captured_argv(argv, timeout_seconds=PSQL_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(
            f"psql failed while re-establishing the {plan.database_username!r} credential "
            f"(exit {result.returncode}): {_decode_both_streams(result)}"
        )


@dataclass(frozen=True)
class CredentialAdoptionResult:
    """What :func:`reset_cluster_credential` accomplished -- a non-secret
    detail line the CLI journals/logs (never the password or the URL)."""

    detail: str


def reset_cluster_credential(
    context: ProvisionContext,
    plan: ProvisionPlan,
    *,
    pg_ctl_path: str,
    psql_path: str,
) -> CredentialAdoptionResult:
    """N-15: re-establish the service-role credential on a surviving,
    product-owned PostgreSQL cluster so a reinstall over preserved data
    produces a working station -- WITHOUT re-initializing or dropping any
    data.

    Thin execution seam (untested directly, same HARD RULE as
    :func:`default_ensure_database` -- no real ``pg_ctl``/``psql`` in the unit
    suite; the live proof belongs to the WP2/WP5 lifecycle matrix). Sequence:

    1. Write a TRANSIENT loopback-``trust`` ``pg_hba.conf``
       (:func:`~civiccast.native.provision.conf.render_pg_hba_trust_conf`) so
       the cluster can be entered as its own bootstrap superuser role without
       the unrecoverable password.
    2. ``pg_ctl start`` (through :func:`_start_provisioned_cluster`, which
       re-asserts the pgdata DACL first, exactly like every other install-time
       start).
    3. VERIFY the cluster is genuinely CivicCast-provisioned
       (:func:`_verify_adoptable_cluster`) -- else
       :class:`AdoptionForeignClusterError`, never a takeover.
    4. ``ALTER ROLE ... PASSWORD`` to the fresh ``context.database_password``.
    5. In a ``finally`` that always runs: restore the fail-closed
       scram-sha-256 ``pg_hba.conf`` (so the ``trust`` grant never outlives
       this window, even on a raised error), then ``pg_ctl stop`` (the same
       "never left running" contract :func:`default_ensure_database` holds).

    After this returns, the caller drives the normal provisioning engine over
    the SAME cluster: the existing cluster is detected and REUSED (no initdb),
    the existing database is detected and REUSED (no CREATE), and every
    downstream ``psql``/schema-migration connection authenticates with the
    freshly-established password.
    """

    _atomic_write_text(
        context.postgres_hba_path, render_pg_hba_trust_conf(host=context.postgres_host)
    )
    _start_provisioned_cluster(pg_ctl_path, context)
    try:
        _verify_adoptable_cluster(psql_path, context, plan)
        _reset_role_password(psql_path, context, plan)
        return CredentialAdoptionResult(
            detail=(
                f"re-established the {plan.database_username!r} credential on the surviving "
                f"PostgreSQL cluster at {context.postgres_data_dir!r} (data preserved; no "
                "initdb, no database drop)"
            )
        )
    finally:
        # Restore the fail-closed scram rule BEFORE stopping, so the transient
        # trust grant never persists on disk regardless of how this block exits
        # (success, foreign-cluster refusal, or a reset fault). Postgres re-reads
        # pg_hba only on reload/restart, so writing it while still up is safe;
        # the freshly-set scram verifier is what the supervisor's later start
        # authenticates against.
        _atomic_write_text(
            context.postgres_hba_path, render_pg_hba_conf(host=context.postgres_host)
        )
        _run_pg_ctl(_pg_ctl_argv_stop(pg_ctl_path, context), action="stop")


def run_schema_migration_to_head(
    *, database_url: str, install_root: str, state_root: str, owner_run_id: str
) -> None:
    """C1 fix (2026-07-31): bring a database's schema to alembic head using
    the SAME runner the D3 upgrade orchestrator uses --
    :func:`civiccast.native.upgrade.seams.default_migrate` (in-process
    ``alembic.command.upgrade(cfg, "head")``, orchestrator step 5) -- never
    a second hand-rolled alembic invocation.

    Why this exists: on a FIRST-EVER install the NSIS hook chain runs the D3
    upgrade engine (the only alembic runner in the product) BEFORE D4
    provisioning and deliberately SKIPS it ("fresh-install gate",
    ``nsis-hooks-bootstrap.nsh``) because there is no database yet -- so
    without this call NOTHING ever creates the tables, the control plane
    serves over an empty schema, and the first alert INSERT crashes the
    supervisor. The app itself only diagnoses schema currency at startup
    (``check_schema_currency``); it never auto-migrates by design.

    In-process (no subprocess -- the C2 bounded-execution rule for
    install-time children does not apply) and idempotent (alembic skips
    already-applied revisions), so a re-provision/repair re-run is safe.
    """

    from civiccast.native.upgrade.models import UpgradeContext
    from civiccast.native.upgrade.seams import default_migrate

    upgrade_context = UpgradeContext(
        install_root=install_root,
        state_root=state_root,
        database_url=database_url,
        owner_run_id=owner_run_id,
    )
    default_migrate(upgrade_context)()


def migrate_provisioned_schema(
    context: ProvisionContext,
    *,
    pg_ctl_path: str,
    database_url: str,
    install_root: str,
) -> None:
    """Start the just-provisioned (stopped) cluster, migrate its schema to
    alembic head, and stop it again unconditionally (``finally``) -- the
    same "never left running" start/act/stop contract
    :func:`default_ensure_database` already holds, because provisioning's
    engine stops postgres at the end of its DATABASE_READY step and the
    supervisor service owns every later start. ``pg_ctl`` runs through the
    proven file-backed bounded executor (:func:`_run_pg_ctl`); a stop
    failure after a successful migration still propagates fail-loud."""

    _start_provisioned_cluster(pg_ctl_path, context)
    try:
        run_schema_migration_to_head(
            database_url=database_url,
            install_root=install_root,
            state_root=context.state_root,
            owner_run_id=context.owner_run_id,
        )
    finally:
        _run_pg_ctl(_pg_ctl_argv_stop(pg_ctl_path, context), action="stop")


def build_default_seams_for(
    plan: ProvisionPlan,
    context: ProvisionContext,
    *,
    public_key: Ed25519PublicKey,
    initdb_path: str = "initdb",
) -> ProvisionSeams:
    """Assemble the production seam bundle for ``plan`` + ``context``.

    ``public_key`` is the Ed25519 public key the server-binaries pack must
    verify against (the same embedded trust root every other native
    component pack uses -- see :mod:`civiccast.installer.native_packs`).

    ``pg_ctl.exe``/``psql.exe`` (BLOCKER #52's ``ensure_database`` seam) are
    resolved as ``initdb_path``'s SIBLINGS in the same staged
    ``native-server-binaries`` pack bin directory -- the exact convention
    :func:`civiccast.native.upgrade.pg_lifecycle.derive_pg_lifecycle_paths`
    already established for ``pg_ctl.exe`` (both binaries are listed
    alongside ``initdb.exe`` in :mod:`civiccast.native.runtime_licenses`'s
    server-binaries pack inventory), not a second staged-directory
    convention invented here.
    """

    def _verify_pack() -> None:
        verify_server_binaries_pack(
            context.server_pack_path,
            public_key=public_key,
            expected_product_version=plan.server_pack_product_version,
            expected_compatible_core=plan.server_pack_compatible_core,
            expected_signing_key_id=plan.server_pack_signing_key_id,
        )

    pg_ctl_path = pg_ctl_path_for(initdb_path)
    psql_path = psql_path_for(initdb_path)

    return ProvisionSeams(
        verify_pack=_verify_pack,
        detect_postgres_cluster=default_detect_postgres_cluster(context),
        run_initdb=default_run_initdb(
            context, initdb_path=initdb_path, database_username=plan.database_username
        ),
        write_postgres_conf=default_write_postgres_conf(context),
        write_pg_hba_conf=default_write_pg_hba_conf(context),
        ensure_database=default_ensure_database(
            context, plan, pg_ctl_path=pg_ctl_path, psql_path=psql_path
        ),
    )


__all__ = [
    "ICACLS_TIMEOUT_SECONDS",
    "INITDB_TIMEOUT_SECONDS",
    "PSQL_TIMEOUT_SECONDS",
    "AdoptionForeignClusterError",
    "CredentialAdoptionResult",
    "alter_role_password_sql",
    "build_default_seams_for",
    "default_ensure_database",
    "initdb_argv",
    "migrate_provisioned_schema",
    "pg_ctl_path_for",
    "psql_path_for",
    "reset_cluster_credential",
    "run_schema_migration_to_head",
]
