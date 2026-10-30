# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Shared hard-ceiling + missing-database classification for DB connect seams
(task #55, consolidating audit-lite findings 2-4 + follow-up #53).

Before this module existed, :mod:`civiccast.native.upgrade.pg_lifecycle`
carried BOTH of these primitives, but scoped ONLY to its own reachability
pre-check (:func:`~civiccast.native.upgrade.pg_lifecycle.real_database_reachable`):

* a hard wall-clock ceiling on a connect attempt, enforced independently of
  whatever the driver/OS does with a stalled socket (BLOCKER #52's measured
  finding: psycopg v3's own ``connect_timeout`` does NOT reliably bound a
  blackholed connect on this platform -- ~130s observed on a refused/
  unroutable endpoint).
* classification of psycopg's ``InvalidCatalogName`` / SQLSTATE ``3D000``
  ("database does not exist") as a distinct, actionable condition --
  :class:`DatabaseMissingError` -- never ordinary unreachability (starting
  postgres or retrying can never fix a database D4 provisioning never
  created).

``civiccast/schema_check.py``'s ``read_db_revision`` -- the SAME
``schema_revision`` seam's real implementation -- had NEITHER: its
``create_engine(..., connect_args={"connect_timeout": 10})`` carries only the
hint the pg_lifecycle module's own docstring already proved this platform's
psycopg v3 does not reliably honor, and a missing-database
``OperationalError`` there was completely unclassified, reaching the D3
engine's ``orchestrator.py`` (its very first, UNGUARDED
``seams.schema_revision()`` call at phase 0) and ``__main__.py``'s generic
"unexpected fault" exit 40 -- indistinguishable from a random bug, instead of
BLOCKER #52's actionable "re-run native provisioning" message.

This module extracts both primitives so EVERY real connect-and-classify seam
(``pg_lifecycle.real_database_reachable`` and ``schema_check.read_db_revision``
today) shares one implementation instead of two independently-drifting ones.
It imports only stdlib -- no native-only dependency (winreg, pywin32,
subprocess-of-a-Windows-binary) -- so ``import civiccast.db.guarded_connect``
succeeds on Linux, matching the house cross-platform-import rule
(:mod:`civiccast.native.win_probes`'s module docstring). ``psycopg`` is a hard
runtime dependency of this project on every platform (ADR 0008), so
:func:`classify_missing_database` imports it directly rather than lazily.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Callable


class DatabaseMissingError(RuntimeError):
    """A caught DBAPI error was classified (:func:`classify_missing_database`)
    as psycopg's ``InvalidCatalogName`` / SQLSTATE ``3D000`` -- the target
    database named in a connection URL does not exist. This is NEVER ordinary
    unreachability: postgres answered, so starting it (already running) or
    blindly retrying the same connection can never fix this -- only creating
    the database can. Each call site raises its own instance with a message
    naming its own actionable next step (e.g. "re-run native provisioning");
    this base class exists so callers can catch ONE type regardless of which
    guarded seam raised it.
    """


def classify_missing_database(exc: BaseException) -> bool:
    """True iff ``exc`` (or the DBAPI cause it wraps) is psycopg's
    ``InvalidCatalogName`` / SQLSTATE ``3D000`` -- "database does not exist".

    Grounded on the REAL driver exception class and its ``sqlstate``
    attribute (both verified against the installed ``psycopg`` package),
    never a string match on an error message: message text is not a stable
    contract, and a substring match could false-positive on an unrelated
    error that happens to mention the database's name. SQLAlchemy wraps the
    original DBAPI exception in ``.orig`` on its own ``DBAPIError``
    subclasses (e.g. ``OperationalError``) -- checked directly, then via one
    ``.orig`` hop, so this works whether ``exc`` is the raw psycopg exception
    or the SQLAlchemy wrapper ``engine.connect()`` actually raises.
    """

    for candidate in (exc, getattr(exc, "orig", None)):
        if candidate is None:
            continue
        if getattr(candidate, "sqlstate", None) == "3D000":
            return True
        try:
            import psycopg.errors as psycopg_errors

            if isinstance(candidate, psycopg_errors.InvalidCatalogName):
                return True
        except ImportError:  # pragma: no cover - psycopg is a hard dependency here
            pass
    return False


def run_bounded[T](fn: Callable[[], T], ceiling_seconds: float) -> T:
    """Run ``fn()`` on a worker thread, bounded by a HARD wall-clock ceiling
    this function itself enforces -- independent of whatever the callee (a
    driver, the OS TCP stack, ...) does with a stalled operation.

    Returns ``fn()``'s result, or propagates whatever ``fn()`` itself raises,
    when it completes within ``ceiling_seconds``. Raises
    ``concurrent.futures.TimeoutError`` (identical to the builtin
    ``TimeoutError`` on this project's Python baseline) when the ceiling
    elapses first.

    A thread still blocked when the ceiling is reached is ABANDONED (its
    eventual result, if any, is discarded; nothing ever joins it) rather than
    waited on -- joining an abandoned thread would just reintroduce the exact
    unbounded wait this function exists to close. This is the measured,
    disclosed fix for BLOCKER #52 (psycopg v3's own ``connect_timeout`` hint
    was observed NOT to reliably bound a blackholed connect on this
    platform); every caller of this function inherits that same hard bound
    regardless of what its own ``fn`` does internally.
    """

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        return future.result(timeout=ceiling_seconds)
    finally:
        # wait=False is load-bearing -- see the docstring above.
        pool.shutdown(wait=False)


__all__ = [
    "DatabaseMissingError",
    "classify_missing_database",
    "run_bounded",
]
