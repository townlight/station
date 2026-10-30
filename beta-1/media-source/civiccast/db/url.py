# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Normalize a bare ``postgresql://`` DATABASE_URL to name the psycopg v3
driver (beta BLOCKER #51).

The defect this fixes: SQLAlchemy maps the driver-less ``postgresql://``
scheme to the ``psycopg2`` dialect (``create_engine``'s default dialect
selection for that scheme), but this project ships ONLY psycopg v3
(``psycopg[binary]>=3.2``, ADR 0008, ``docs/adr/0008-database-session-pattern.md``)
-- ``psycopg2`` is never installed. The installer persists the native
product's Postgres credential under the plain ``postgresql://`` scheme (the
Rust writer, ``native_service_registration.rs``'s ``write_database_url``; see
``civiccast/native/supervisor/service_env.py``), so every consumer of that
persisted value that builds a SQLAlchemy engine or ``Config`` crashed with
``ModuleNotFoundError: No module named 'psycopg2'`` the moment it tried to
connect (Sandbox gauntlet run 13, service crash + D3 update-path fault).

Neither of the codebase's existing "working" connection paths
(:mod:`civiccast.db.session`, used by ``civiccast/app.py``'s FastAPI wiring)
normalizes the scheme either -- they pass ``DATABASE_URL`` straight into
``create_engine`` unchanged. The default first-mile install path never hits
this because it provisions a local SQLite database
(:mod:`civiccast.installer.storage`); the bug is latent there too whenever an
operator points ``DATABASE_URL`` at a bare ``postgresql://`` Postgres
instance, but reproducing it live requires the native-Postgres path this
blocker is scoped to. No existing normalizer was found anywhere in the tree
(``grep -rn '+psycopg' civiccast/`` returns nothing outside this module and
its callers) -- this is a new, small, dependency-free helper, not a
consolidation of an existing pattern.

Applied at every create_engine/make_url consumer of the installer-persisted
``DatabaseUrl`` in the supervisor + D3 upgrade engine:

* :func:`civiccast.native.supervisor.service.default_dependency_provider`
* :func:`civiccast.native.upgrade.pg_lifecycle.real_database_reachable` and
  its ``make_url`` use in :func:`civiccast.native.upgrade.pg_lifecycle.
  derive_pg_lifecycle_paths`
* :func:`civiccast.native.upgrade.service_control._real_snapshot_digest`
* :func:`civiccast.native.upgrade.seams.default_migrate` (the alembic
  ``Config.sqlalchemy.url`` it sets)
* :func:`civiccast.schema_check.read_db_revision` -- shared by
  :func:`civiccast.native.upgrade.seams.default_schema_revision` (the D3
  engine's first DB touch) AND ``civiccast/app.py``'s startup
  ``check_schema_currency`` call, so this one fix covers both without
  touching either caller directly.

Also applied, as a follow-up sweep after the original BLOCKER #51 fix
shipped, at the one call site that fix's "out of scope" note above used to
name and that crash-looped the native control plane at import
(``ModuleNotFoundError: No module named 'psycopg2'``, Sandbox run 19):

* :func:`civiccast.app._create_database_engine` -- the durable-store engine
  wired by ``civiccast/app.py``'s ``_install_durable_store_wiring``
  (``_create_database_engine``'s non-sqlite branch). This call site
  predates the native port and never got the BLOCKER #51 normalization.

``civiccast/dr/backup.py`` was normalized separately (its own comment,
Sandbox run 16 row-4b) and is not in this module's blast radius either --
noted here only because an earlier revision of this docstring still listed
it as unfixed; that was stale.

FIXED, no longer out of scope (2026-08-01 audit follow-up correction): an
earlier revision of this docstring listed ``civiccast/cli.py``'s
``_bind_egress_database`` here as "still out of scope" -- that went stale.
It was normalized in freeze v6 and now calls ``normalize_database_url``
(grep ``_bind_egress_database`` in ``civiccast/cli.py``: its
``create_engine`` call wraps ``normalize_database_url(database_url)``, the
same as every other call site above).

NOTHING IS OUT OF SCOPE ANY MORE (chain K/K1, 2026-08-01). An earlier revision
of this docstring listed three modules as "still out of scope ... outside this
blocker's named blast radius". One of those three was WRONG on the facts and
cost a real-hardware install:

* ``civiccast/dr/restore_drill.py`` was described as "operator/DR-drill only,
  not on the native service/control-plane/installer path". It is squarely ON
  that path: D3 step 3 (``BACKUP_VERIFIED``) calls
  :func:`civiccast.dr.restore_drill.run_postgres_restore_drill` as its
  pre-upgrade restore-drill spot check
  (:func:`civiccast.native.upgrade.seams.default_backup`), and that function's
  first statement built an engine on the raw registry URL. Real-hardware R7,
  2026-08-01: the upgrade rolled back at exactly that call with
  ``No module named 'psycopg2'``. Every ``create_engine`` in that module now
  goes through its own ``_verification_engine_url`` wrapper.
* ``civiccast/captions/tap_worker.py`` and
  ``civiccast/live/finalization_worker.py``'s ``main()`` entrypoints were
  correctly described (external-process-worker mode, which the native
  supervisor never spawns) but are normalized anyway, so the shipped-wheel
  guard below can be absolute instead of carrying exceptions.

The class is now closed structurally, not case by case:
``tests/policy/test_shipped_payload_db_driver.py`` walks the AST of EVERY
module in the shipped ``civiccast`` package and fails on any
``create_engine``/``make_url`` whose URL is not derived from this function (or
from a literal whose scheme already names a driver). Verified sensitive: run
against the pre-fix tree it reports the exact R7 call site. Add a new engine
anywhere in the wheel without normalizing and that guard fails before the
installer ever ships.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

#: The scheme SQLAlchemy maps to the (uninstalled) psycopg2 dialect when no
#: driver is named.
_PLAIN_POSTGRES_DRIVERNAME = "postgresql"

#: The driver this project actually ships (ADR 0008).
_PSYCOPG_V3_DRIVERNAME = f"{_PLAIN_POSTGRES_DRIVERNAME}+psycopg"


def normalize_database_url(database_url: str) -> str:
    """Rewrite a bare ``postgresql://`` URL to ``postgresql+psycopg://``.

    * A URL that already names ANY driver (``postgresql+psycopg://``,
      ``postgresql+psycopg2://``, ``postgresql+asyncpg://``, ...) is returned
      completely UNCHANGED -- an explicit operator/deployment driver choice
      always wins over this normalization.
    * A non-Postgres scheme (``sqlite://``, ...) is returned unchanged.
    * The password is preserved in plaintext in the returned string --
      ``sqlalchemy.engine.URL.__str__``/``repr`` hide the password by
      default (render as ``***``), so this uses
      ``render_as_string(hide_password=False)`` deliberately. Silently
      corrupting the credential here would be worse than the bug this
      fixes: every normalized URL must still authenticate.

    Raises whatever :func:`sqlalchemy.engine.make_url` raises on a
    malformed URL -- the same failure ``create_engine`` would eventually
    raise anyway, just surfaced one call earlier.
    """

    parsed = make_url(database_url)
    if parsed.drivername != _PLAIN_POSTGRES_DRIVERNAME:
        return database_url
    return parsed.set(drivername=_PSYCOPG_V3_DRIVERNAME).render_as_string(hide_password=False)


__all__ = ["normalize_database_url"]


def postgres_connect_args(url: str, *, connect_timeout_seconds: int) -> dict[str, int]:
    """``connect_args`` for :func:`sqlalchemy.create_engine`: a psycopg
    ``connect_timeout`` for postgres URLs, EMPTY for everything else.

    psycopg v3 with no ``connect_timeout`` can hang minutes on Windows on a
    refused/blackholed connect (measured ~130s, task #51) -- but the same
    keyword is an invalid argument for sqlite3 connections, and shared seams
    like ``schema_check.read_db_revision`` serve BOTH the native postgres
    lane and the WSL/sqlite lane. Conditioning here keeps every caller
    one-line safe.
    """

    if make_url(url).get_backend_name() == "postgresql":
        return {"connect_timeout": connect_timeout_seconds}
    return {}
