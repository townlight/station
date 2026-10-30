# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real backup: a physical database snapshot + a media manifest, not a proof token.

Two engines are supported, dispatched from ``DATABASE_URL``:

* **SQLite** (the default managed-storage deployment): the SQLite backup API
  (``sqlite3.Connection.backup``) produces a consistent point-in-time copy
  even while the source file is open elsewhere — never a raw
  ``shutil.copy2`` of a live db file, which can copy a half-written page.
* **Postgres** (the technical-deployment option): ``pg_dump`` in custom
  format via subprocess, driven off the parsed ``DATABASE_URL``.

Table row counts and content checksums are computed generically over
SQLAlchemy (:func:`snapshot_tables`) so the same code verifies both engines —
the restore drill re-runs the identical function against the restored copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from civiccast.dr.models import (
    BackupManifest,
    IntegrityManifestEntry,
    MediaManifestEntry,
    TableSnapshot,
)

_ROW_SEPARATOR = "\x1e"  # ASCII record separator; won't collide with real column text
_FIELD_SEPARATOR = "\x1f"  # ASCII unit separator

#: Task #55's hard wall-clock ceiling (civiccast.db.guarded_connect.run_bounded)
#: for this module's own Postgres connects -- same 15s bound as
#: schema_check.read_db_revision's connect seam. psycopg v3 without
#: connect_timeout can hang minutes on Windows on a refused/blackholed
#: connect (measured, task #51/#52); the ceiling is enforced here
#: independent of whatever the driver/OS does with a stalled socket.
_BACKUP_CONNECT_CEILING_SECONDS = 15.0


def _schema_for(engine: Engine) -> str | None:
    """The 'civiccast' schema qualifies Postgres tables; SQLite has no schemas."""

    return None if engine.dialect.name == "sqlite" else "civiccast"


def build_sqlite_engine(db_path: Path) -> Engine:
    """A SQLite engine that resolves ``civiccast.*``-qualified ORM tables correctly.

    Mirrors ``civiccast.app._create_database_engine``: ``Base.metadata`` binds
    every model to ``schema="civiccast"`` (ADR 0008), and production maps that
    prefix away for SQLite via ``schema_translate_map`` rather than the
    ``ATTACH DATABASE ':memory:' AS civiccast`` connect-hook that
    ``civiccast.db.base`` registers for the fast in-memory test path. Building
    a plain ``create_engine(f"sqlite:///...")`` here (without the translate
    map) would silently query the wrong (attached, empty) schema and read
    zero rows — this exists so the drill reads a real on-disk database the
    same way the running app does, not the way an in-memory test does.
    """

    from sqlalchemy import create_engine as _create_engine

    engine = _create_engine(f"sqlite:///{db_path}", future=True)
    return engine.execution_options(schema_translate_map={"civiccast": None})


def snapshot_tables(engine: Engine, *, connection: Connection | None = None) -> list[TableSnapshot]:
    """Row count + deterministic content checksum for every app table.

    Generic over dialect: works identically against the live station database
    at backup time and against a freshly restored database at verify time, so
    the restore drill is comparing apples to apples. Ordered by each table's
    primary key (falls back to every column, in declared order, for a
    PK-less table) so the checksum is stable regardless of physical row
    order.

    ``connection``, when given, is read from AS-IS instead of opening a new
    ``engine.connect()`` -- the caller already holds an open transaction
    (e.g. one bound to an exported ``pg_export_snapshot()`` id, see
    :func:`run_full_backup`'s Postgres branch), and a FRESH connection here
    would see a different (later) database state than that transaction's own
    MVCC snapshot, defeating the entire point of exporting it.
    """

    schema = _schema_for(engine)
    inspector = inspect(engine)
    table_names = sorted(inspector.get_table_names(schema=schema))
    snapshots: list[TableSnapshot] = []

    def _snapshot_through(conn: Connection) -> None:
        for name in table_names:
            if name == "alembic_version":
                continue  # verified separately via schema_check, not row-diffed
            qualified = f'"{schema}"."{name}"' if schema else f'"{name}"'
            pk_cols = inspector.get_pk_constraint(name, schema=schema).get(
                "constrained_columns"
            ) or [col["name"] for col in inspector.get_columns(name, schema=schema)]
            order_by = ", ".join(f'"{c}"' for c in pk_cols)
            rows = conn.execute(text(f"SELECT * FROM {qualified} ORDER BY {order_by}")).fetchall()  # noqa: S608 -- identifiers from inspector, not user input  # nosec B608
            hasher = hashlib.sha256()
            for row in rows:
                hasher.update(
                    _FIELD_SEPARATOR.join(
                        "\x00NULL\x00" if v is None else str(v) for v in row
                    ).encode("utf-8", errors="surrogateescape")
                )
                hasher.update(_ROW_SEPARATOR.encode())
            snapshots.append(
                TableSnapshot(name=name, row_count=len(rows), checksum_sha256=hasher.hexdigest())
            )

    if connection is not None:
        _snapshot_through(connection)
    else:
        with engine.connect() as conn:
            _snapshot_through(conn)
    return snapshots


def run_sqlite_backup(*, db_path: Path, dest_dir: Path) -> Path:
    """Consistent point-in-time snapshot of a SQLite database via the backup API.

    Never a raw file copy of a live db — SQLite's backup API takes its own
    read lock and copies page-by-page, so it is safe against concurrent
    writers (the app keeps running during a backup).
    """

    dest_dir.mkdir(parents=True, exist_ok=True)
    artifact = dest_dir / "database.sqlite3"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(artifact))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return artifact


def _spawn_pg_tool(
    argv: list[str],
    *,
    tool: str,
    env: dict[str, str],
    timeout: int,
    stdin_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """``subprocess.run(argv, ...)``, but a failed SPAWN names the executable.

    Windows raises ``FileNotFoundError: [WinError 2] The system cannot find
    the file specified`` with ``filename`` unset and no argv anywhere in the
    message when ``argv[0]`` cannot be resolved. Sandbox run 22 (2026-07-31)
    recorded exactly that string as the D3 upgrade engine's terminal error,
    and it identified neither which of the four PostgreSQL client tools had
    failed nor the path attempted -- the real cause (a bare ``pg_dump``
    resolved through a PATH that never contains the staged pack ``bin``
    directory) cost a full gauntlet run to reconstruct from source.

    The replacement message names the tool, ``argv[0]``, and whether that was
    an absolute path that is not on disk or a bare name that PATH could not
    resolve -- the two have different fixes. It stays a
    :class:`FileNotFoundError` so existing handling is unaffected, and sets
    ``filename`` so structured consumers get the path too.

    ONLY ``argv[0]`` is quoted into the message. ``argv[1:]`` carries the
    host, port, user and database name parsed out of ``DATABASE_URL`` and is
    deliberately never echoed; the password never reaches argv at all (it is
    passed via ``PGPASSWORD`` in ``env``).
    """

    executable = argv[0] if argv else ""
    try:
        return subprocess.run(  # noqa: S603 -- fixed arg list, no shell, args from parsed DATABASE_URL
            argv,
            input=stdin_bytes,
            env=env,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        how = (
            "an absolute path that is not present on disk"
            if Path(executable).is_absolute()
            else "a bare command name that could not be resolved through PATH"
        )
        # Built as OSError(errno, strerror) with ``filename`` set, so ``str()``
        # renders "[Errno 2] <diagnosis>: '<argv[0]>'" -- passing the message
        # alone and THEN setting ``filename`` makes OSError.__str__ drop the
        # message entirely. The path is interpolated plainly (not ``!r``) so a
        # Windows path in the diagnosis is not backslash-doubled.
        error = FileNotFoundError(
            exc.errno,
            f'{tool} could not be started: the executable "{executable}" is '
            f"{how}. Original OS error: {exc.strerror or exc}",
        )
        error.filename = executable
        raise error from exc


def _parse_postgres_url(database_url: str) -> dict[str, str]:
    parts = urlsplit(database_url)
    return {
        "host": parts.hostname or "localhost",
        "port": str(parts.port or 5432),
        "user": unquote(parts.username) if parts.username else "",
        "password": unquote(parts.password) if parts.password else "",
        "dbname": unquote(parts.path.lstrip("/")),
    }


def run_postgres_backup(
    *,
    database_url: str,
    dest_dir: Path,
    pg_dump_command: list[str] | None = None,
    snapshot_id: str | None = None,
) -> Path:
    """``pg_dump`` (custom format) snapshot of a Postgres database.

    The dump is captured from pg_dump's STDOUT and written locally, so the
    command can be a plain local ``pg_dump`` (the default) OR a prefixed one
    (e.g. ``docker exec -i <container> pg_dump``) whose filesystem we never
    touch — that is also how the CI gate dodges client/server version skew
    forever: the server dumps itself. Station databases are small (metadata,
    not media), so stdout capture is fine; raises :class:`RuntimeError` with
    the captured stderr on failure rather than silently producing an
    empty/corrupt dump.

    ``snapshot_id`` (from ``SELECT pg_export_snapshot()`` on a held
    REPEATABLE READ transaction -- see :func:`run_full_backup`'s Postgres
    branch) binds this dump to that EXACT exported MVCC snapshot via
    ``pg_dump --snapshot=<id>``, the same snapshot the manifest's table
    checksums are read from. The manifest and the dump artifact then
    describe the identical database state BY CONSTRUCTION, not by a
    before/after assumption. Works identically with a ``docker exec``
    command prefix: a snapshot id is a server-side identifier, not a
    filesystem path.
    """

    dest_dir.mkdir(parents=True, exist_ok=True)
    artifact = dest_dir / "database.pgdump"
    conn = _parse_postgres_url(database_url)
    env = dict(os.environ)
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]
    argv = [
        *(pg_dump_command or ["pg_dump"]),
        "--host",
        conn["host"],
        "--port",
        conn["port"],
        "--username",
        conn["user"],
        "--format",
        "custom",
        "--no-password",
        *(("--snapshot", snapshot_id) if snapshot_id else ()),
        conn["dbname"],
    ]
    result = _spawn_pg_tool(argv, tool="pg_dump", env=env, timeout=600)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"pg_dump failed (exit {result.returncode}): {stderr}")
    if not result.stdout:
        raise RuntimeError("pg_dump produced an empty dump — refusing to write it.")
    artifact.write_bytes(result.stdout)
    return artifact


def run_postgres_globals_backup(
    *,
    database_url: str,
    dest_dir: Path,
    pg_dumpall_command: list[str] | None = None,
) -> Path:
    """``pg_dumpall --globals-only`` capture of cluster-global roles.

    :func:`run_postgres_backup` runs ``pg_dump``, which is scoped to ONE
    database and never captures cluster-global objects -- roles chief among
    them. Without this artifact, a restore onto a cluster that does not
    already happen to have the source's roles would fail role-dependent
    statements (ownership, grants) the moment they were replayed, or worse,
    restore silently under the wrong role. ``globals.sql`` makes role
    capture part of the backup set instead of an unwritten assumption.
    Same command-prefix + ``PGPASSWORD`` + raise-on-failure pattern as
    :func:`run_postgres_backup` -- ``pg_dumpall`` needs ``--host``/
    ``--port``/``--username`` like ``pg_dump`` does, but never a dbname
    argument: it operates across the whole cluster, not one database.
    """

    dest_dir.mkdir(parents=True, exist_ok=True)
    artifact = dest_dir / "globals.sql"
    conn = _parse_postgres_url(database_url)
    env = dict(os.environ)
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]
    argv = [
        *(pg_dumpall_command or ["pg_dumpall"]),
        "--host",
        conn["host"],
        "--port",
        conn["port"],
        "--username",
        conn["user"],
        "--no-password",
        "--globals-only",
    ]
    result = _spawn_pg_tool(argv, tool="pg_dumpall --globals-only", env=env, timeout=600)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"pg_dumpall --globals-only failed (exit {result.returncode}): {stderr}")
    if not result.stdout:
        raise RuntimeError(
            "pg_dumpall --globals-only produced empty output — refusing to write it."
        )
    artifact.write_bytes(result.stdout)
    return artifact


def run_postgres_restore(
    artifact_path: Path,
    target_database_url: str,
    *,
    pg_restore_command: list[str] | None = None,
    preserve_ownership: bool = False,
    single_transaction: bool = False,
    no_acls: bool = False,
    clean_if_exists: bool = False,
) -> None:
    """Restore a ``pg_dump --format=custom`` artifact into ``target_database_url``.

    Custom-format dumps support restoring from stdin, so this feeds the
    artifact's bytes to ``pg_restore`` the same way :func:`run_postgres_backup`
    captures ``pg_dump``'s stdout -- and accepts the same command-prefix
    pattern (a plain local ``pg_restore``, or a prefixed one such as
    ``docker exec -i <container> pg_restore``) so in-container execution
    works identically for both directions of the drill. ``--exit-on-error``
    turns the first restore error into a nonzero exit rather than a
    silently-partial restore. Raises :class:`RuntimeError` with the captured
    stderr on failure -- including a truncated/corrupt artifact, which is
    exactly the failure mode the drill exists to catch.

    ``preserve_ownership=False`` (the default) passes ``--no-owner``,
    dropping role-ownership statements -- correct for the SAME-CLUSTER
    restore drill's throwaway target
    (:func:`civiccast.dr.restore_drill.run_postgres_restore_drill`), which
    does not own (and must not assume) the source roles.
    ``preserve_ownership=True`` drops ``--no-owner`` so ``pg_restore``
    replays the dump's ownership (``ALTER ... OWNER TO ...``) statements --
    only correct against a cluster whose roles were already replayed via
    ``globals.sql`` first, which is exactly
    :func:`civiccast.dr.restore_drill.run_postgres_cold_standby_drill`'s own
    precondition (it replays globals.sql and verifies the roles exist BEFORE
    calling this with ``preserve_ownership=True``).

    ``clean_if_exists=True`` asks pg_restore to drop objects represented in
    the archive before recreating them. Station replacement uses this with a
    single transaction so a provisioned replacement schema can be restored
    without requiring a separately empty database.
    """

    conn = _parse_postgres_url(target_database_url)
    env = dict(os.environ)
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]
    argv = [
        *(pg_restore_command or ["pg_restore"]),
        "--host",
        conn["host"],
        "--port",
        conn["port"],
        "--username",
        conn["user"],
        "--dbname",
        conn["dbname"],
        *(() if preserve_ownership else ("--no-owner",)),
        *(("--no-acl",) if no_acls else ()),
        *(("--clean", "--if-exists") if clean_if_exists else ()),
        # <installer-path-audit BL-01> ``--single-transaction`` wraps the whole
        # replay in ONE transaction, so a failure part-way through rolls the
        # target back to empty instead of leaving it half-restored. Opt-in
        # rather than default because the drill's throwaway target does not
        # need it and a single transaction is a real cost on a large dump; the
        # D3 rollback -- which restores into the LIVE database -- always asks
        # for it, because "half-clobbered production" is the failure mode that
        # matters there. It implies --exit-on-error, which is passed anyway so
        # the non-transactional path keeps its existing behaviour.
        *(("--single-transaction",) if single_transaction else ()),
        "--exit-on-error",
        "--no-password",
    ]
    result = _spawn_pg_tool(
        argv,
        tool="pg_restore",
        env=env,
        timeout=600,
        stdin_bytes=artifact_path.read_bytes(),
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"pg_restore failed (exit {result.returncode}): {stderr}")


@dataclass(frozen=True)
class DatabaseLocale:
    """A database's encoding + collation, for a comparable clone (MN-08)."""

    encoding: str
    lc_collate: str
    lc_ctype: str


#: What a database inherits when its own row cannot be read.
#:
#: NOT necessarily what this product's own provisioning creates --
#: civiccast/native/provision/seams.py's ``initdb`` passed no
#: ``-E``/``--encoding``/``--locale`` before this defect was fixed, so any
#: cluster provisioned before then is the OS codepage (WIN1252 on Windows),
#: not UTF8. Only a cluster initdb'd AFTER that fix (``initdb_argv`` pins
#: ``--encoding UTF8 --locale C`` for NEW clusters only) is actually UTF8/C.
#: This constant is used ONLY when :func:`read_database_locale` cannot read
#: the SOURCE database's real row at all (old server, permission refusal, a
#: database that does not exist yet) -- the normal path always clones the
#: source's MEASURED encoding/collation via ``pg_encoding_to_char(encoding)``
#: (see :func:`read_database_locale`), never this constant. UTF8 + C remains
#: the right pair to fall back to when the real value is unreadable: UTF8 can
#: encode anything (never a worse choice for a fresh clone with no better
#: information), and C is byte-ordering -- deterministic on every platform, no
#: locale pack required -- so a fallback clone still orders rows identically
#: to itself, even if it may not match a differently-collated source.
_FALLBACK_DATABASE_LOCALE = DatabaseLocale(encoding="UTF8", lc_collate="C", lc_ctype="C")

#: Characters a Postgres encoding / locale name can contain. Anything else is
#: refused rather than interpolated into DDL.
_LOCALE_TOKEN = re.compile(r"^[A-Za-z0-9_.:@ +-]{1,64}$")


def read_database_locale(
    *,
    database_url: str,
    source_database_name: str,
    psql_command: list[str] | None = None,
) -> DatabaseLocale:
    """Read ``source_database_name``'s encoding/collation from ``pg_database``.

    Never raises: an unreadable row (an old server, a permission refusal, a
    database that does not exist yet) yields
    :data:`_FALLBACK_DATABASE_LOCALE`, because failing a backup over a
    cosmetic-looking metadata read would be worse than cloning with the safe
    default. Every value is validated against :data:`_LOCALE_TOKEN` before it
    is interpolated into DDL -- these strings come from the server, not from a
    caller, but they still end up in a ``CREATE DATABASE`` statement.
    """

    if not _LOCALE_TOKEN.match(source_database_name):
        # The name comes from the caller's OWN connection URL, but it still
        # reaches a SQL literal below; refuse anything that is not a plain
        # identifier rather than quoting defensively.
        return _FALLBACK_DATABASE_LOCALE
    conn = _parse_postgres_url(database_url)
    env = dict(os.environ)
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]
    query = f"SELECT pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database WHERE datname = '{source_database_name}'"  # noqa: S608 -- name validated against _LOCALE_TOKEN immediately above  # nosec B608
    argv = [
        *(psql_command or ["psql"]),
        "--host",
        conn["host"],
        "--port",
        conn["port"],
        "--username",
        conn["user"],
        "--no-password",
        "--dbname",
        "postgres",
        "--tuples-only",
        "--no-align",
        "--field-separator",
        "|",
        "-c",
        query,
    ]
    try:
        result = _spawn_pg_tool(argv, tool="psql (source database locale)", env=env, timeout=60)
    except Exception:
        return _FALLBACK_DATABASE_LOCALE
    if result.returncode != 0:
        return _FALLBACK_DATABASE_LOCALE
    line = result.stdout.decode("utf-8", "replace").strip().splitlines()
    if not line:
        return _FALLBACK_DATABASE_LOCALE
    parts = line[0].split("|")
    if len(parts) != 3 or not all(_LOCALE_TOKEN.match(part.strip()) for part in parts):
        return _FALLBACK_DATABASE_LOCALE
    return DatabaseLocale(
        encoding=parts[0].strip(), lc_collate=parts[1].strip(), lc_ctype=parts[2].strip()
    )


def create_fresh_postgres_database(
    *,
    database_url: str,
    database_name: str = "civiccast_drill_restore",
    psql_command: list[str] | None = None,
    allow_dropping_the_connection_url_database: bool = False,
) -> str:
    """Drop-if-exists then create ``database_name`` on the same server; return its URL.

    <installer-path-audit MN-09> ``DROP DATABASE ... WITH (FORCE)`` runs
    against the PRODUCTION cluster, and nothing used to refuse when
    ``database_name`` equalled the database ``database_url`` itself addresses:
    a station provisioned with the default drill name would have lost
    production data to a drill. That guard now exists, and
    ``allow_dropping_the_connection_url_database`` is the ONE deliberate
    opt-out -- the D3 rollback restore (installer-path audit BL-01), which
    must drop and recreate the live database so ``pg_restore`` replays into an
    empty target, and which does so under the held D7a maintenance interlock
    with a verified backup in hand.

    Idempotent on both ends of the drop/create pair: ``DROP DATABASE IF
    EXISTS ... WITH (FORCE)`` never fails if the drill database is already
    gone, or has stale connections left over from a prior aborted drill run
    (``WITH (FORCE)`` disconnects them -- Postgres 13+); ``CREATE DATABASE``
    then always starts the restore from a genuinely empty database, not one
    that might still carry rows from a previous drill. Runs against the
    ``postgres`` maintenance database because ``DROP``/``CREATE DATABASE``
    cannot execute against the database being dropped/created. Uses the same
    command-prefix pattern as :func:`run_postgres_backup`/
    :func:`run_postgres_restore` so ``psql`` can run locally or inside the
    same container that hosts the server.
    """

    conn = _parse_postgres_url(database_url)
    if database_name == conn["dbname"] and not allow_dropping_the_connection_url_database:
        raise RuntimeError(
            f"refusing to DROP DATABASE {database_name!r}: it is the database this connection "
            "URL addresses, so this call would destroy the very data it is meant to protect. "
            "Pass allow_dropping_the_connection_url_database=True only from a caller that "
            "deliberately means to replace the live database (the D3 rollback restore)."
        )
    env = dict(os.environ)
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]
    locale = read_database_locale(
        database_url=database_url, source_database_name=conn["dbname"], psql_command=psql_command
    )
    argv = [
        *(psql_command or ["psql"]),
        "--host",
        conn["host"],
        "--port",
        conn["port"],
        "--username",
        conn["user"],
        "--no-password",
        "--dbname",
        "postgres",
        "--set",
        "ON_ERROR_STOP=1",
        "-c",
        f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)',
        "-c",
        # <installer-path-audit MN-08> A bare `CREATE DATABASE` inherits
        # template1's encoding and collation. On a Windows-installed cluster
        # template1 is commonly SQL_ASCII/C while the product's own database is
        # UTF-8, and `snapshot_tables`' primary-key ORDER BY then differs
        # between source and copy -- surfacing as an unexplained checksum
        # mismatch on an otherwise perfect backup, which (because the drill
        # gates the pre-upgrade backup) fails the whole upgrade. Clone the
        # SOURCE database's own settings from template0 instead, so the copy is
        # comparable by construction. This matters even more for BL-01's
        # live-database recreate, where a collation change would be a real,
        # silent change to production.
        f'CREATE DATABASE "{database_name}" TEMPLATE template0 '
        f"ENCODING '{locale.encoding}' LC_COLLATE '{locale.lc_collate}' "
        f"LC_CTYPE '{locale.lc_ctype}'",
    ]
    result = _spawn_pg_tool(argv, tool="psql (drill database DROP/CREATE)", env=env, timeout=120)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"create fresh drill database failed (exit {result.returncode}): {stderr}"
        )

    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database_name}", "", ""))


def build_media_manifest(
    media_root: Path, *, sample_bytes: int = 65536
) -> list[MediaManifestEntry]:
    """Bounded-sample fingerprint of every file under ``media_root``.

    Hashing terabytes of media on every drill is not a drill, it is an
    outage — so this hashes a bounded head+tail sample per file, not the
    whole file. It proves the manifest matches what is on disk *today*
    (size + sampled content); it is not a substitute for the operator's own
    file-level media backup.
    """

    entries: list[MediaManifestEntry] = []
    if not media_root.exists():
        return entries
    for path in sorted(p for p in media_root.rglob("*") if p.is_file()):
        size = path.stat().st_size
        hasher = hashlib.sha256()
        half = sample_bytes // 2
        with path.open("rb") as fh:
            head = fh.read(half)
            hasher.update(head)
            hashed = len(head)
            if size > half:
                tail_start = max(half, size - half)
                fh.seek(tail_start)
                tail = fh.read()
                hasher.update(tail)
                hashed += len(tail)
        entries.append(
            MediaManifestEntry(
                path=str(path.relative_to(media_root)),
                size_bytes=size,
                sampled_sha256=hasher.hexdigest(),
                sample_bytes=hashed,
            )
        )
    return entries


def write_integrity_manifest(dest_dir: Path) -> list[IntegrityManifestEntry]:
    """sha256 of every file that shipped inside the backup set (full-file, small artifacts)."""

    entries: list[IntegrityManifestEntry] = []
    for path in sorted(p for p in dest_dir.rglob("*") if p.is_file() and p.name != "manifest.json"):
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                hasher.update(chunk)
        entries.append(
            IntegrityManifestEntry(
                member=str(path.relative_to(dest_dir)), sha256=hasher.hexdigest()
            )
        )
    return entries


def read_backup_manifest(dest_dir: Path) -> BackupManifest:
    """Parse ``manifest.json`` out of a backup directory.

    Extracted so a consumer that must TRUST a backup before acting on it (the
    D3 rollback restore) reads the manifest through one validated path rather
    than re-implementing the parse.
    """

    manifest_path = dest_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"backup manifest not found at {manifest_path}")
    return BackupManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def verify_backup_integrity(dest_dir: Path, manifest: BackupManifest) -> list[str]:
    """Re-hash every member the manifest names; return a list of discrepancies.

    <installer-path-audit MA-09> ``BackupRef.verified`` was
    ``bool(manifest.integrity)`` -- a NON-EMPTINESS check, not a verification --
    while the orchestrator gated on it and wrote the journal detail
    "hash + restore-drill spot check". ``_manifest_blob_hash`` folds the
    manifest's OWN recorded hashes, so both sides of any later tamper check
    came from the same manifest, and no file's bytes were ever re-hashed
    anywhere in the product (a grep for ``write_integrity_manifest`` /
    ``.integrity`` / ``IntegrityManifestEntry`` across ``civiccast/`` found
    only the write sites, the ``bool(...)``, and a recovery-document print).
    A dump truncated AFTER the manifest was written therefore passed
    ``verified=True``, and the only thing that would have caught it -- the
    drill -- restores the same bytes.

    Returns an empty list when every member is present and matches. Never
    raises for a mismatch; the caller decides what a mismatch means (the D3
    rollback refuses to drop a live database over it, the backup path fails
    the backup).
    """

    errors: list[str] = []
    for entry in manifest.integrity:
        member = dest_dir / entry.member
        if not member.is_file():
            errors.append(f"{entry.member}: missing from the backup directory")
            continue
        hasher = hashlib.sha256()
        with member.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(chunk)
        actual = hasher.hexdigest()
        if actual != entry.sha256:
            errors.append(
                f"{entry.member}: sha256 {actual} does not match the manifest's {entry.sha256}"
            )
    if not manifest.integrity:
        errors.append(
            "the backup manifest carries no integrity entries at all, so nothing about these "
            "bytes has been verified"
        )
    return errors


def _assert_backup_quiescent(before: list[TableSnapshot], after: list[TableSnapshot]) -> None:
    """Raise if the database changed between the exported snapshot and the post-dump read.

    Redocumented for Fix CC-WS2-003: ``run_full_backup``'s manifest is no
    longer bound to the dump by an unverified BEFORE/AFTER assumption --
    :func:`run_full_backup`'s Postgres branch now exports one MVCC snapshot
    (``pg_export_snapshot()``) up front, reads the manifest's table snapshot
    THROUGH that snapshot's own held transaction, and hands the same
    snapshot id to ``pg_dump --snapshot=<id>``. The manifest and the dump
    therefore describe the identical database state BY CONSTRUCTION: no
    write landing after the snapshot was exported -- however it interleaves
    with the dump, including an insert-then-delete (ABA) sequence that a
    before/after row-count-and-checksum diff could never distinguish from
    "nothing happened" -- can be seen by one side and not the other, because
    neither side can see it at all.

    This function's remaining job is DIFFERENT from what it used to be: it
    is no longer the mechanism that binds the manifest to the dump (that is
    now structural), it detects whether the *environment* the backup ran in
    was actually quiescent. ``before`` is the exported-snapshot read (what
    the manifest and the dump both describe); ``after`` is a FRESH
    post-dump read through a brand-new connection. A difference here means a
    write landed after the snapshot was exported -- harmless to the
    manifest/artifact binding, but a sign the "quiescent maintenance window"
    an operator may be relying on elsewhere did not actually hold. Still a
    hard fail: that assumption should get fixed, not silently waved through.
    """

    before_by_name = {t.name: t for t in before}
    after_by_name = {t.name: t for t in after}
    if before_by_name.keys() != after_by_name.keys():
        raise RuntimeError(
            "database table set changed during backup (before="
            f"{sorted(before_by_name)!r} after={sorted(after_by_name)!r}) — "
            "the manifest cannot be bound to this dump"
        )
    changed = sorted(
        name
        for name, b in before_by_name.items()
        if after_by_name[name].row_count != b.row_count
        or after_by_name[name].checksum_sha256 != b.checksum_sha256
    )
    if changed:
        raise RuntimeError(
            f"database changed during backup (tables: {', '.join(changed)}) — "
            "the manifest cannot be bound to this dump"
        )


def run_full_backup(
    *,
    database_url: str,
    dest_dir: Path,
    media_root: Path | None = None,
    config_paths: Iterable[Path] = (),
    engine_for_snapshot: Engine | None = None,
    command_database_url: str | None = None,
    pg_dump_command: list[str] | None = None,
    pg_dumpall_command: list[str] | None = None,
) -> BackupManifest:
    """Back up the database + media manifest + config, and write ``manifest.json``.

    ``engine_for_snapshot`` lets callers (tests, or a caller that already has
    an engine bound) reuse an existing connection instead of opening a new
    one purely to compute the table snapshot.

    ``command_database_url`` (Postgres only) is the URL ``pg_dump``/
    ``pg_dumpall`` parse for their own ``--host``/``--port``/``--username``
    argv -- the in-container view when ``pg_dump_command``/
    ``pg_dumpall_command`` are a ``docker exec`` prefix. It defaults to
    ``database_url``, which stays the SQLAlchemy view this function's own
    connections (the snapshot, below) always use. The two views legitimately
    differ whenever the command runs inside a container that sees the
    server as ``localhost`` while this process reaches it through a
    host-mapped port.
    """

    from sqlalchemy import create_engine

    from civiccast.db import connect_options
    from civiccast.db.guarded_connect import run_bounded
    from civiccast.db.url import normalize_database_url

    dest_dir.mkdir(parents=True, exist_ok=True)
    backup_id = f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"

    globals_artifact_name: str | None = None
    if database_url.startswith("sqlite"):
        db_path = Path(database_url.split("///", 1)[1])
        artifact = run_sqlite_backup(db_path=db_path, dest_dir=dest_dir)
        # <installer-path-audit MA-10> Snapshot the SOURCE, not the artifact.
        #
        # This used to read the manifest's table snapshot out of the COPY
        # (`build_sqlite_engine(artifact)`), and the restore drill then
        # `shutil.copy2`s that same artifact and re-snapshots it -- so the
        # whole `_table_results` comparison was artifact-vs-copy-of-artifact.
        # A `run_sqlite_backup` that silently produced a copy diverging from
        # the live database (a partial page copy, a source opened at the wrong
        # path, a schema_translate_map regression yielding zero or blank rows)
        # recorded whatever the artifact contained and the drill compared it to
        # itself and reported PASSED. SQLite is this module's own documented
        # "default managed-storage deployment". The Postgres branch below
        # already does the right thing, reading through an exported
        # `pg_export_snapshot()` transaction.
        snapshot_engine = engine_for_snapshot or build_sqlite_engine(db_path)
        tables = snapshot_tables(snapshot_engine)
        if engine_for_snapshot is None:
            snapshot_engine.dispose()
        engine_name = "sqlite"
    elif database_url.startswith("postgresql"):
        dump_url = command_database_url or database_url
        # normalize_database_url: a bare `postgresql://` scheme maps to the
        # (uninstalled) psycopg2 dialect -- this project ships psycopg v3
        # only (ADR 0008, beta BLOCKER #51). #51's own normalization pass
        # grepped only native/ + alerting/ and missed this module (Sandbox
        # run 16 row-4b: the backup step crashed "No module named
        # 'psycopg2'" AFTER writers_drained) -- applied here at both of this
        # branch's engine constructions. ``pg_dump``/``pg_dumpall`` below are
        # NOT touched: they parse ``dump_url`` themselves via
        # ``_parse_postgres_url`` (plain ``urlsplit``, never SQLAlchemy), so
        # the psycopg dialect is irrelevant to them.
        snapshot_engine = engine_for_snapshot or create_engine(
            normalize_database_url(database_url), **connect_options(database_url)
        )
        # Fix CC-WS2-003: bind the manifest and the dump to ONE exported
        # MVCC snapshot instead of diffing two separate reads. Hold a
        # REPEATABLE READ transaction open, export its snapshot id, read the
        # manifest's table snapshot THROUGH that same transaction/connection
        # (snapshot_tables' connection= param), then hand the id to pg_dump
        # so it reads the IDENTICAL view -- see _assert_backup_quiescent's
        # docstring for why this removes the ABA race a before/after diff
        # could only ever detect, never prevent.
        #
        # Task #55's hard wall-clock ceiling (civiccast.db.guarded_connect.
        # run_bounded), same 15s bound as schema_check.read_db_revision's
        # own connect seam: psycopg v3 without connect_timeout can hang
        # minutes on Windows on a refused/blackholed connect (measured,
        # task #51/#52), and this drill's connect is exactly that kind of
        # seam -- never a query against an already-open connection.
        snapshot_conn = run_bounded(
            lambda: snapshot_engine.connect().execution_options(isolation_level="REPEATABLE READ"),
            _BACKUP_CONNECT_CEILING_SECONDS,
        )
        try:
            snapshot_id = snapshot_conn.execute(text("SELECT pg_export_snapshot()")).scalar_one()
            tables = snapshot_tables(snapshot_engine, connection=snapshot_conn)
            artifact = run_postgres_backup(
                database_url=dump_url,
                dest_dir=dest_dir,
                pg_dump_command=pg_dump_command,
                snapshot_id=snapshot_id,
            )
        finally:
            snapshot_conn.rollback()
            snapshot_conn.close()

        # Defense-in-depth, redocumented (see _assert_backup_quiescent): no
        # longer guards the manifest/dump binding itself (that is now
        # structural), only the operator's quiescent-window assumption.
        post_check_engine = create_engine(
            normalize_database_url(database_url), **connect_options(database_url)
        )
        try:
            post_snapshot = run_bounded(
                lambda: snapshot_tables(post_check_engine), _BACKUP_CONNECT_CEILING_SECONDS
            )
        finally:
            post_check_engine.dispose()
        _assert_backup_quiescent(tables, post_snapshot)

        globals_artifact_name = run_postgres_globals_backup(
            database_url=dump_url, dest_dir=dest_dir, pg_dumpall_command=pg_dumpall_command
        ).name
        if engine_for_snapshot is None:
            snapshot_engine.dispose()
        engine_name = "postgres"
    else:
        raise ValueError(f"Unsupported DATABASE_URL scheme for backup: {database_url!r}")

    media_entries = build_media_manifest(media_root) if media_root is not None else []
    config_files = []
    for config_path in config_paths:
        if config_path.exists():
            dest = dest_dir / "config" / config_path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_path, dest)
            config_files.append(str(dest.relative_to(dest_dir)))

    manifest = BackupManifest(
        backup_id=backup_id,
        created_at=datetime.now(UTC),
        engine=engine_name,
        db_artifact=artifact.name,
        tables=tables,
        media_entries=media_entries,
        config_files=config_files,
        globals_artifact=globals_artifact_name,
    )
    (dest_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2, exclude={"integrity"}), encoding="utf-8"
    )
    integrity = write_integrity_manifest(dest_dir)
    manifest = manifest.model_copy(update={"integrity": integrity})
    (dest_dir / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest
