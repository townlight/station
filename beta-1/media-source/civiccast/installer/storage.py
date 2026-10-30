# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Managed local durable storage for first-mile installs.

This module is the non-technical operator path for storage bootstrap. If a
station has not supplied DATABASE_URL, CivicCast provisions a local durable
SQLite database, applies the same Alembic migration graph, and records the
result in a small installer-owned state file. Technical admins can still point
DATABASE_URL at Postgres; the default path no longer asks meeting staff to do
that work by hand.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, NamedTuple

from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict, Field

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_CONFIG_NAME = "managed-storage.json"
_DB_NAME = "civiccast.sqlite3"
_UPLOADS_DIR_NAME = "uploads"
_LOCK_NAME = ".managed-storage.lock"
_LOCK_TIMEOUT_SECONDS = 60.0

#: ``durable_storage_status`` statuses that only the EXTERNAL ``DATABASE_URL``
#: branch can produce. The managed/SQLite branch
#: (:func:`managed_storage_status`) produces only ``"ready"``,
#: ``"needs_attention"``, and ``"not_configured"`` and is untouched by this
#: module's external-database probe. Callers that render operator copy branch
#: on this set so the managed path keeps its own wording verbatim.
EXTERNAL_DATABASE_READY = "ready"
EXTERNAL_DATABASE_UNREACHABLE = "unreachable"
EXTERNAL_DATABASE_SCHEMA_BEHIND = "schema_behind"
#: <installer-path-audit MA-06> The database was migrated by a NEWER build
#: than the one running. Its own status because the remedy is the opposite of
#: ``schema_behind``'s: running the migrations cannot help, since this build's
#: graph does not contain the revision the database is stamped with.
EXTERNAL_DATABASE_SCHEMA_AHEAD = "schema_ahead"
EXTERNAL_DATABASE_MISSING = "database_missing"
EXTERNAL_DATABASE_MISCONFIGURED = "misconfigured"
EXTERNAL_DATABASE_NOT_READY_STATUSES = frozenset(
    {
        EXTERNAL_DATABASE_UNREACHABLE,
        EXTERNAL_DATABASE_SCHEMA_BEHIND,
        EXTERNAL_DATABASE_SCHEMA_AHEAD,
        EXTERNAL_DATABASE_MISSING,
        EXTERNAL_DATABASE_MISCONFIGURED,
    }
)

#: Hard wall-clock ceiling on the whole external-database probe, enforced by
#: :func:`civiccast.db.guarded_connect.run_bounded` (a thread the caller
#: ABANDONS on expiry) rather than by any driver-level timeout hint --
#: BLOCKER #52 measured psycopg v3's own ``connect_timeout`` NOT reliably
#: bounding a blackholed connect on this platform (~130s observed), and
#: :func:`civiccast.schema_check.read_db_revision`'s own inner ceiling is
#: 15s, chosen for the D3 upgrade engine and app startup rather than for an
#: endpoint a GUI polls. ``/installer/summary`` is polled by the installer
#: window (``apps/installer/src/App.tsx`` refreshes on a 2s and a 3s timer),
#: so this seam needs its OWN, tighter bound; the inner 15s constant is
#: deliberately NOT changed, because its other two callers are not on a poll
#: loop and a shorter bound there could abort a legitimate slow upgrade probe.
#:
#: 5 seconds, chosen against MEASURED numbers on this platform rather than
#: intuition -- a stopped database is NOT the fast case it looks like:
#:
#: * healthy database, revision read end to end: ~0.02s (executed).
#: * raw TCP connect to a CLOSED loopback port: 2.05s before Windows reports
#:   ConnectionRefusedError -- not immediate, even on 127.0.0.1.
#: * ``read_db_revision`` against that same closed port: 10.47s, because
#:   psycopg keeps retrying until its own ``connect_timeout=10`` hint expires.
#:
#: So the ordinary "the operator has not started Postgres yet" case ALREADY
#: exceeds any acceptable budget for a polled endpoint; this ceiling is what
#: turns that 10.47s into a bounded 5s, and a blackholed endpoint's ~130s
#: (BLOCKER #52's measurement) into the same 5s. 5s rather than something
#: tighter because a refused connect alone costs 2.05s here, and a cold remote
#: connect (DNS + TLS + auth) needs headroom above that before CivicCast is
#: entitled to call a merely-slow database unreachable.
_EXTERNAL_DATABASE_PROBE_CEILING_SECONDS = 5.0

#: How long one probe result is reused. Without this, the 2s GUI poll would
#: start a fresh 5s probe faster than the previous one can finish whenever the
#: database is blackholed, and ``run_bounded`` ABANDONS the thread it gives up
#: on -- the endpoint would stay bounded but the process would accumulate one
#: stuck connect thread every poll. 10s keeps at most one probe in flight in
#: that state while still turning the readiness screen green within one
#: operator glance after Postgres actually starts.
_EXTERNAL_DATABASE_PROBE_TTL_SECONDS = 10.0


class ManagedStorageError(RuntimeError):
    """Raised when installer-managed durable storage cannot be prepared."""


class ManagedStorageStatus(BaseModel):
    """Current installer-managed durable storage state."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1)
    database_url: str = Field(min_length=1)
    database_path: str = Field(min_length=1)
    upload_dir: str = Field(min_length=1)
    storage_dir: str = Field(min_length=1)
    migrations_applied: bool
    configured_at: datetime
    operator_message: str = Field(min_length=1)
    next_step: str = Field(min_length=1)

    def report(self) -> ManagedStorageStatusReport:
        """The credential-free view of this state that every API route serves.

        SECURITY (walkthrough on a running beta.5 station, 2026-09-09): the
        ``database_url`` above is the live connection string, and on a native
        station it carries the ``civiccast_svc`` PostgreSQL password. It was
        being returned verbatim by ``GET /api/setup/storage`` to any caller on
        the station's loopback with no Authorization header at all -- so any
        local process or user could open the station database. Only this
        report ever leaves the process; the URL itself stays in memory for the
        engine binding (``civiccast.app._resolve_database_url``).
        """

        kind, host, port, name = _describe_database_url(self.database_url)
        return ManagedStorageStatusReport(
            status=self.status,
            database_configured=True,
            database_kind=kind,
            database_host=host,
            database_port=port,
            database_name=name,
            database_path=self.database_path,
            upload_dir=self.upload_dir,
            storage_dir=self.storage_dir,
            migrations_applied=self.migrations_applied,
            configured_at=self.configured_at,
            operator_message=self.operator_message,
            next_step=self.next_step,
        )


class ManagedStorageStatusReport(BaseModel):
    """Durable storage state as served over the API: never the credential.

    Same shape as :class:`ManagedStorageStatus` minus ``database_url``, which
    is replaced by ``database_configured`` plus the backend kind and, for a
    network database, its host, port and database name. No user name, no
    password, no URL -- see :meth:`ManagedStorageStatus.report`.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1)
    database_configured: bool
    database_kind: str = Field(min_length=1)
    database_host: str | None = None
    database_port: int | None = None
    database_name: str | None = None
    database_path: str = Field(min_length=1)
    upload_dir: str = Field(min_length=1)
    storage_dir: str = Field(min_length=1)
    migrations_applied: bool
    configured_at: datetime
    operator_message: str = Field(min_length=1)
    next_step: str = Field(min_length=1)


_DESCRIBABLE_DATABASE_KINDS = frozenset(
    {"postgresql", "postgres", "sqlite", "mysql", "mariadb", "mssql", "oracle"}
)


def _describe_database_url(database_url: str) -> tuple[str, str | None, int | None, str | None]:
    """Return ``(kind, host, port, database_name)`` for a URL, never its credential.

    Two fail-closed rules keep an operator-set ``DATABASE_URL`` from leaking
    through the descriptive fields (MAJOR-1, hostile review of PR #215):

    * The kind is only ever a whitelisted backend name or ``"unknown"`` --
      never a slice of the input. The old exception path returned the text
      before the first ``:`` verbatim, which for a value pasted without its
      scheme is the user name or the password.
    * A URL whose part after ``://`` carries more than one ``@`` describes
      nothing but its kind. SQLAlchemy's URL grammar captures the password
      as ``[^@]*``, so an unescaped ``@`` inside the password splits at the
      wrong ``@`` and the password tail lands in ``host`` (or ``database``).
      An escaped ``%40`` password has exactly one ``@`` and is described
      normally. A ``?password=a@b`` query is the one clean shape this also
      withholds -- deliberately: withholding a host is cheap, echoing a
      credential is not.
    """

    scheme, separator, remainder = database_url.partition("://")
    kind = scheme.split("+", 1)[0].lower() if separator else ""
    if kind not in _DESCRIBABLE_DATABASE_KINDS:
        kind = "unknown"
    if kind == "sqlite" or kind == "unknown":
        return kind, None, None, None
    if remainder.count("@") > 1:
        return kind, None, None, None
    try:
        from sqlalchemy.engine import make_url

        from civiccast.db.url import normalize_database_url

        # Descriptive only (no engine is built here), but every make_url in
        # the shipped wheel goes through the normalizer -- the policy tripwire
        # in tests/policy/test_shipped_payload_db_driver.py is textual, by
        # design, so the class of bug that rolled back R7 cannot creep back.
        parsed = make_url(normalize_database_url(database_url))
    except Exception:
        return kind, None, None, None
    host = parsed.host
    if host is not None and "@" in host:
        return kind, None, None, None
    return kind, host, parsed.port, parsed.database


def default_storage_dir() -> Path:
    """Return the station-local data directory for managed storage."""

    configured = os.environ.get("CIVICCAST_MANAGED_STORAGE_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "CivicCast"
    return Path.home() / ".local" / "share" / "civiccast"


def sqlite_database_url(path: Path) -> str:
    """Build a SQLAlchemy SQLite URL for an absolute path."""

    return f"sqlite:///{path.resolve().as_posix()}"


def managed_storage_status(storage_dir: Path | None = None) -> ManagedStorageStatus:
    """Read managed storage state without creating new storage."""

    root = _resolve_storage_dir(storage_dir)
    config = _read_config(root)
    if config is None:
        db_path = _database_path(root)
        upload_dir = _upload_dir(root)
        return ManagedStorageStatus(
            status="not_configured",
            database_url=sqlite_database_url(db_path),
            database_path=str(db_path),
            upload_dir=str(upload_dir),
            storage_dir=str(root),
            migrations_applied=False,
            configured_at=datetime.now(UTC),
            operator_message="CivicCast has not prepared local durable storage yet.",
            next_step="Choose Prepare storage in the installer.",
        )
    db_path = Path(str(config["database_path"]))
    upload_dir = Path(str(config.get("upload_dir") or _upload_dir(root)))
    migrations_applied = bool(config.get("migrations_applied")) and db_path.exists()
    uploads_ready = upload_dir.exists()
    ready = migrations_applied and uploads_ready
    return ManagedStorageStatus(
        status="ready" if ready else "needs_attention",
        database_url=str(config["database_url"]),
        database_path=str(db_path),
        upload_dir=str(upload_dir),
        storage_dir=str(root),
        migrations_applied=migrations_applied,
        configured_at=datetime.fromisoformat(str(config["configured_at"])),
        operator_message=(
            "CivicCast local durable storage is ready."
            if ready
            else "CivicCast found managed storage config, but local storage proof is incomplete."
        ),
        next_step=(
            "Open the operator console and continue setup."
            if ready
            else "Choose Prepare storage again so CivicCast can repair local storage."
        ),
    )


class ExternalDatabaseProbe(NamedTuple):
    """One executed answer about the database ``DATABASE_URL`` names.

    Four operator-distinct outcomes, never collapsed into one status:

    * ``ready`` -- the database answered AND its alembic revision is the head
      this build expects.
    * ``schema_behind`` -- the database answered, but its revision is not the
      expected head (including no ``alembic_version`` table at all). This is
      the worst masked case the old unconditional ``status="ready"`` hid:
      auth works, tokens work, and endpoints touching newer columns 500.
    * ``unreachable`` -- the connect failed or exceeded
      :data:`_EXTERNAL_DATABASE_PROBE_CEILING_SECONDS`.
    * ``database_missing`` -- Postgres answered but the named database does
      not exist (SQLSTATE ``3D000``), classified by
      :func:`civiccast.db.guarded_connect.classify_missing_database` and
      re-raised by :func:`civiccast.schema_check.read_db_revision` as
      :class:`~civiccast.db.guarded_connect.DatabaseMissingError`. Starting or
      retrying Postgres can never fix this; only provisioning can.
    * ``misconfigured`` -- ``DATABASE_URL`` cannot be parsed at all, or this
      build cannot determine the migration head it expects. Both carry their
      own ``operator_message``, so the two are distinguishable on screen even
      though they share a status.
    """

    status: str
    migrations_applied: bool
    operator_message: str
    next_step: str


def _probe_external_database(database_url: str) -> ExternalDatabaseProbe:
    """Execute a bounded connectivity + schema-currency check. Never raises.

    Reuses the repo's proven helpers rather than re-deriving any of them:
    :func:`civiccast.db.url.normalize_database_url` (the bare-``postgresql://``
    -> psycopg v3 driver fix, BLOCKER #51),
    :func:`civiccast.db.guarded_connect.run_bounded` (the hard wall-clock
    ceiling BLOCKER #52 proved the driver's own ``connect_timeout`` cannot
    provide on this platform), and :mod:`civiccast.schema_check`'s
    ``read_db_revision`` / ``expected_migration_head`` /
    ``evaluate_schema_currency`` (the existing alembic-head implementation --
    no head logic is re-derived here).
    """

    import concurrent.futures

    from sqlalchemy.exc import ArgumentError

    from civiccast.db.guarded_connect import DatabaseMissingError, run_bounded
    from civiccast.db.url import normalize_database_url
    from civiccast.schema_check import (
        evaluate_schema_currency,
        expected_migration_head,
        read_db_revision,
    )

    try:
        normalize_database_url(database_url)
    except ArgumentError:
        return ExternalDatabaseProbe(
            status=EXTERNAL_DATABASE_MISCONFIGURED,
            migrations_applied=False,
            operator_message=(
                "CivicCast cannot read the database address this computer was set "
                "up with. The address is not in a form CivicCast understands, so "
                "CivicCast cannot store meeting records."
            ),
            next_step=(
                "Ask whoever set up this computer to correct its database address, "
                "restart CivicCast, then reopen this window."
            ),
        )

    try:
        expected_head = expected_migration_head()
    except Exception:
        return ExternalDatabaseProbe(
            status=EXTERNAL_DATABASE_MISCONFIGURED,
            migrations_applied=False,
            operator_message=(
                "CivicCast could not work out which database version this copy of "
                "CivicCast expects, so it cannot confirm the database is ready."
            ),
            next_step=(
                "This CivicCast installation is incomplete. Reinstall CivicCast on "
                "this computer, then reopen this window."
            ),
        )

    try:
        db_revision = run_bounded(
            lambda: read_db_revision(database_url),
            _EXTERNAL_DATABASE_PROBE_CEILING_SECONDS,
        )
    except DatabaseMissingError:
        return ExternalDatabaseProbe(
            status=EXTERNAL_DATABASE_MISSING,
            migrations_applied=False,
            operator_message=(
                "The database server answered, but the CivicCast database on it "
                "does not exist. Starting or restarting the database server cannot "
                "fix this."
            ),
            next_step=(
                "Ask whoever set up this computer to create the CivicCast database "
                "on that server, then reopen this window."
            ),
        )
    except concurrent.futures.TimeoutError:
        return ExternalDatabaseProbe(
            status=EXTERNAL_DATABASE_UNREACHABLE,
            migrations_applied=False,
            operator_message=(
                "CivicCast could not reach its database. The database did not "
                "answer within "
                f"{int(_EXTERNAL_DATABASE_PROBE_CEILING_SECONDS)} seconds, so "
                "CivicCast cannot store meeting records yet."
            ),
            next_step=(
                "Make sure the database server is running and reachable from this "
                "computer, then reopen this window."
            ),
        )
    except Exception:
        return ExternalDatabaseProbe(
            status=EXTERNAL_DATABASE_UNREACHABLE,
            migrations_applied=False,
            operator_message=(
                "CivicCast could not reach its database, so it cannot store meeting records yet."
            ),
            next_step=(
                "Make sure the database server is running and reachable from this "
                "computer, then reopen this window."
            ),
        )

    from civiccast.schema_check import known_revisions

    try:
        known = known_revisions()
    except Exception:  # pragma: no cover - a branched graph; classify as before
        known = None
    state = evaluate_schema_currency(db_revision, expected_head, known=known).state
    if state == "current":
        return ExternalDatabaseProbe(
            status=EXTERNAL_DATABASE_READY,
            migrations_applied=True,
            operator_message="Durable database storage is configured and ready.",
            next_step="Open the operator console and continue setup.",
        )
    if state == "ahead":
        # <installer-path-audit MA-06> A NEWER CivicCast migrated this
        # database. The "bring the database up to date" advice below cannot
        # work here: `alembic upgrade head` cannot locate a revision this
        # build's graph does not contain, so it simply fails. This is also
        # exactly the state a failed rollback leaves behind (new schema, old
        # binary), so it is not a hypothetical.
        return ExternalDatabaseProbe(
            status=EXTERNAL_DATABASE_SCHEMA_AHEAD,
            migrations_applied=False,
            operator_message=(
                "CivicCast reached its database, but the database was set up by a NEWER "
                "version of CivicCast than the one running here. Meeting records saved now "
                "could be lost or rejected."
            ),
            next_step=(
                "Install the newer version of CivicCast again on this computer, or ask "
                "whoever set it up to restore the backup taken before the upgrade. Updating "
                "the database will NOT fix this."
            ),
        )
    return ExternalDatabaseProbe(
        status=EXTERNAL_DATABASE_SCHEMA_BEHIND,
        migrations_applied=False,
        operator_message=(
            "CivicCast reached its database, but the database is set up for an "
            "older version of CivicCast. Meeting records saved now could be lost "
            "or rejected."
        ),
        next_step=(
            "Ask whoever set up this computer to bring the CivicCast database up "
            "to date, then reopen this window."
        ),
    )


#: Single-entry, time-boxed memo of the last probe -- see
#: :data:`_EXTERNAL_DATABASE_PROBE_TTL_SECONDS`. Deliberately unlocked: a race
#: costs one duplicate probe, while a lock would let a second request wait for
#: an in-flight probe AND then run its own, doubling the worst-case latency
#: this seam exists to bound.
_external_database_probe_cache: dict[str, tuple[float, ExternalDatabaseProbe]] = {}


def reset_external_database_probe_cache() -> None:
    """Drop the memoized probe result (tests, and any deliberate re-check)."""

    _external_database_probe_cache.clear()


def _cached_probe_external_database(database_url: str) -> ExternalDatabaseProbe:
    now = time.monotonic()
    cached = _external_database_probe_cache.get(database_url)
    if cached is not None and cached[0] > now:
        return cached[1]
    probe = _probe_external_database(database_url)
    _external_database_probe_cache.clear()
    _external_database_probe_cache[database_url] = (
        now + _EXTERNAL_DATABASE_PROBE_TTL_SECONDS,
        probe,
    )
    return probe


def durable_storage_status(storage_dir: Path | None = None) -> ManagedStorageStatus:
    """Return external DATABASE_URL or managed local storage state.

    The external branch used to return ``status="ready"``,
    ``migrations_applied=True`` UNCONDITIONALLY the moment ``DATABASE_URL`` was
    set, with no connection attempt and no schema check. On the native Windows
    product ``DATABASE_URL`` is ALWAYS set (the supervisor hydrates it from
    HKLM), so every readiness lane derived from this function reported green on
    a station whose database was stopped, missing, or migrations behind. It now
    executes :func:`_probe_external_database` instead.

    The managed/SQLite branch below is UNCHANGED: this function still returns
    :func:`managed_storage_status` verbatim both when ``DATABASE_URL`` matches
    the managed URL and when it is unset, and no probe runs on that path.
    """

    configured_database_url = os.environ.get("DATABASE_URL")
    if configured_database_url:
        try:
            managed = managed_storage_status(storage_dir)
        except ManagedStorageError:
            managed = None
        if managed is not None and managed.database_url == configured_database_url:
            return managed
        probe = _cached_probe_external_database(configured_database_url)
        return ManagedStorageStatus(
            status=probe.status,
            database_url=configured_database_url,
            database_path="configured by DATABASE_URL",
            upload_dir=os.environ.get("CIVICCAST_UPLOAD_DIR", "configured separately"),
            storage_dir="configured by DATABASE_URL",
            migrations_applied=probe.migrations_applied,
            configured_at=datetime.now(UTC),
            operator_message=probe.operator_message,
            next_step=probe.next_step,
        )
    return managed_storage_status(storage_dir)


def ensure_managed_storage(
    *,
    storage_dir: Path | None = None,
    run_migrations: bool = True,
) -> ManagedStorageStatus:
    """Create or repair installer-managed durable storage and run migrations."""

    root = _resolve_storage_dir(storage_dir)
    try:
        _secure_dir(root)
    except OSError as exc:
        raise ManagedStorageError(f"Could not create CivicCast storage directory: {exc}") from exc

    with _storage_lock(root):
        data_dir = root / "data"
        upload_dir = _upload_dir(root)
        db_path = _database_path(root)
        try:
            _secure_dir(data_dir)
            _secure_dir(upload_dir)
        except OSError as exc:
            raise ManagedStorageError(
                f"Could not create CivicCast storage subdirectory: {exc}"
            ) from exc

        database_url = sqlite_database_url(db_path)
        if run_migrations:
            _run_migrations(database_url)
        configured_at = datetime.now(UTC)
        status = ManagedStorageStatus(
            status="ready",
            database_url=database_url,
            database_path=str(db_path),
            upload_dir=str(upload_dir),
            storage_dir=str(root),
            migrations_applied=run_migrations,
            configured_at=configured_at,
            operator_message="CivicCast prepared local durable storage and applied database migrations.",
            next_step="Open the operator console, create the first admin, and save the recovery kit.",
        )
        _write_config(root, status)
        return status


def load_managed_database_url(storage_dir: Path | None = None) -> str | None:
    """Return the managed DATABASE_URL if a valid managed DB already exists."""

    status = managed_storage_status(storage_dir)
    if status.status == "ready":
        return status.database_url
    return None


def load_managed_upload_dir(storage_dir: Path | None = None) -> str | None:
    """Return the managed upload directory if managed storage is ready."""

    status = managed_storage_status(storage_dir)
    if status.status == "ready":
        return status.upload_dir
    return None


def _resolve_storage_dir(storage_dir: Path | None) -> Path:
    return (storage_dir or default_storage_dir()).expanduser().resolve()


def _database_path(storage_dir: Path) -> Path:
    return storage_dir / "data" / _DB_NAME


def _upload_dir(storage_dir: Path) -> Path:
    return storage_dir / _UPLOADS_DIR_NAME


def _config_path(storage_dir: Path) -> Path:
    return storage_dir / _CONFIG_NAME


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(stat.S_IRWXU)


@contextmanager
def _storage_lock(storage_dir: Path) -> Iterator[None]:
    """Serialize managed-storage creation across app workers."""

    lock_path = storage_dir / _LOCK_NAME
    try:
        with lock_path.open("a+b") as lock_file:
            _acquire_lock(lock_file)
            try:
                yield
            finally:
                _release_lock(lock_file)
    except OSError as exc:
        raise ManagedStorageError(f"Could not lock CivicCast managed storage: {exc}") from exc


def _acquire_lock(lock_file: Any) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if os.name == "nt":
                msvcrt: Any = __import__("msvcrt")

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = __import__("fcntl")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise ManagedStorageError(
                    "Timed out waiting for another CivicCast process to finish "
                    "preparing local durable storage."
                ) from exc
            time.sleep(0.1)


def _release_lock(lock_file: Any) -> None:
    if os.name == "nt":
        msvcrt: Any = __import__("msvcrt")

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl: Any = __import__("fcntl")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_config(storage_dir: Path) -> dict[str, Any] | None:
    path = _config_path(storage_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedStorageError(f"Could not read managed storage config: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManagedStorageError("Managed storage config is not a JSON object.")
    return payload


def _write_config(storage_dir: Path, status: ManagedStorageStatus) -> None:
    path = _config_path(storage_dir)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        status.model_dump_json(indent=2),
        encoding="utf-8",
    )
    if os.name != "nt":
        tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    tmp_path.replace(path)
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _run_migrations(database_url: str) -> None:
    if _ALEMBIC_INI.exists():
        cfg = Config(str(_ALEMBIC_INI))
        cfg.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(cfg, "head")
        return
    try:
        with resources.as_file(resources.files("civiccast.alembic") / "alembic.ini") as ini_path:
            package_root = ini_path.parent.parent
            version_locations = [ini_path.parent / "versions"]
            version_locations.extend(sorted(package_root.glob("*/migrations/versions")))
            cfg = Config(str(ini_path))
            cfg.set_main_option("sqlalchemy.url", database_url)
            cfg.set_main_option("script_location", str(ini_path.parent))
            cfg.set_main_option(
                "version_locations", "\n".join(str(path) for path in version_locations)
            )
            command.upgrade(cfg, "head")
    except ModuleNotFoundError as exc:
        raise ManagedStorageError(
            "Could not find Alembic config in the repository or packaged wheel."
        ) from exc
