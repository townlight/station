# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""SQLAlchemy engine factory + FastAPI session dependency.

Per ADR 0008. Sync SQLAlchemy 2.0 + ``psycopg`` v3 in production; tests
bind an ephemeral SQLite engine via :func:`bind_engine`.

Design constraints (any one of these violated would break unrelated
tests at import time):

* The engine is constructed *lazily* inside :func:`get_engine` and
  memoized in a module-level slot. ``import civiccast.db`` MUST succeed
  with ``DATABASE_URL`` unset, because ``app = create_app()`` runs at
  module import in :mod:`civiccast.app` and a transitive import from a
  future router would otherwise crash every unit test.
* :func:`get_session` is a generator with ``try/finally: close()`` so
  FastAPI closes the session at the end of every request, even if the
  handler raises.
* :func:`bind_engine` swaps the memoized engine atomically so test
  fixtures can inject a fresh in-memory SQLite engine per test
  (mirrors the ``dependency_overrides`` pattern used by
  :mod:`tests.vod.test_router`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from civiccast.db.url import normalize_database_url

# Module-level singleton slots. Both default to None and are populated on
# first call to get_engine() (or replaced wholesale by bind_engine()).
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Construct a sessionmaker bound to the given engine."""
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
        class_=Session,
    )


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine, building it on first call.

    Reads ``DATABASE_URL`` from the environment on first call and
    memoizes the resulting :class:`Engine`. Subsequent calls return the
    same instance until :func:`reset_engine` clears it or
    :func:`bind_engine` replaces it.

    Raises :class:`RuntimeError` if ``DATABASE_URL`` is unset *and* no
    engine has been bound via :func:`bind_engine`. Callers that import
    :mod:`civiccast.db` without intending to use the database (the
    common case for unit tests of unrelated modules) never trigger this
    path because they never call :func:`get_engine`.
    """
    global _engine, _session_factory
    if _engine is not None:
        return _engine

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set and no engine has been bound via "
            "civiccast.db.bind_engine(). Set DATABASE_URL in the "
            "environment, or call bind_engine(engine) from a test "
            "fixture before invoking get_engine()."
        )

    url = normalize_database_url(url)
    _engine = create_engine(url, future=True, pool_pre_ping=True, **connect_options(url))
    _session_factory = _build_session_factory(_engine)
    return _engine


DEFAULT_CONNECT_TIMEOUT_SECONDS = 10


def connect_options(url: str, *, timeout_seconds: int | None = None) -> dict[str, object]:
    """Bound how long a dead database may stall a request.

    GauntletGate rc18 PE-2026-07-22-1 measured a resident-facing playback
    request hanging for 6m36s against an unreachable Postgres before the driver
    finally gave up. The 503 that request now returns is worth little if the
    caller waits six minutes to receive it, so the connect attempt is bounded.

    Only applied to PostgreSQL: SQLite is a local file with no connect phase,
    and passing the option to it raises. Override with
    ``CIVICCAST_DB_CONNECT_TIMEOUT`` (seconds); a non-numeric or non-positive
    value falls back to the default rather than disabling the bound.

    ``timeout_seconds`` is a deliberate site-local bound that takes precedence
    over the environment override and the default — for call sites whose
    timing is load-bearing (the native upgrade reachability probe's 5 s bound
    predates this helper and must not drift with global tuning).
    """

    # Matches "postgresql", "postgresql+psycopg", and the legacy "postgres://"
    # scheme; sqlite and any other scheme get no connect bound.
    if not url.startswith("postgres"):
        return {}
    if timeout_seconds is not None and timeout_seconds > 0:
        return {"connect_args": {"connect_timeout": timeout_seconds}}
    raw = os.environ.get("CIVICCAST_DB_CONNECT_TIMEOUT", "").strip()
    timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            timeout = parsed
    return {"connect_args": {"connect_timeout": timeout}}


def bind_engine(engine: Engine) -> None:
    """Replace the process-wide engine and session factory.

    Intended for test fixtures that build an ephemeral SQLite engine
    and want every subsequent ``get_session()`` call to yield against
    it. Production callers should set ``DATABASE_URL`` instead and let
    :func:`get_engine` build the engine itself.
    """
    global _engine, _session_factory
    previous = _engine
    _engine = engine
    _session_factory = _build_session_factory(engine)
    if previous is not None and previous is not engine:
        previous.dispose()


def reset_engine() -> None:
    """Dispose the current engine (if any) and clear both singletons.

    Idempotent: calling on an already-reset state is a no-op. After this
    runs, the next :func:`get_engine` call rebuilds from
    ``DATABASE_URL`` (or raises if unset). Test teardowns call this so
    a per-test SQLite engine does not leak into the next test.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a Session for one request.

    Usage::

        from fastapi import Depends
        from sqlalchemy.orm import Session
        from civiccast.db import get_session

        @router.get("/foo")
        def handler(db: Session = Depends(get_session)) -> ...:
            ...

    Tests substitute the dependency via
    ``app.dependency_overrides[get_session] = lambda: ...`` exactly as
    :mod:`tests.vod.test_router` substitutes ``get_store``.

    The session is closed in ``finally`` so a handler that raises does
    not leak an open session.
    """
    if _session_factory is None:
        # Trigger lazy construction. get_engine() raises a clear error
        # if DATABASE_URL is unset and no engine was bound.
        get_engine()
    assert _session_factory is not None  # invariant: get_engine() builds it
    session = _session_factory()
    try:
        yield session
    finally:
        session.close()
