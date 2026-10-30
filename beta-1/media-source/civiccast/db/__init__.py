# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""civiccast.db — SQLAlchemy session + Alembic foundation.

Public API every other CivicCast module imports from. Stable per ADR
0008; future modules (the schedule module at rung 0.3, the
PostgresAssetStore at task 1b, every later persistence consumer) bind
to this surface unchanged.

Re-exports:

* :class:`Base` — SQLAlchemy 2.0 :class:`DeclarativeBase` carrying
  ``MetaData(schema="civiccast")``. Inherit from this for ORM models.
* :func:`get_engine` — lazy, memoized engine factory.
* :func:`bind_engine` — replace the engine (test injection hook).
* :func:`reset_engine` — dispose + clear (test teardown hook).
* :func:`get_session` — FastAPI dependency yielding a per-request
  :class:`Session` with ``try/finally: close()`` semantics.
"""

from __future__ import annotations

from civiccast.db._alembic_compat import install as install_alembic_compat
from civiccast.db.base import Base
from civiccast.db.session import (
    bind_engine,
    connect_options,
    get_engine,
    get_session,
    reset_engine,
)

__all__ = [
    "Base",
    "bind_engine",
    "connect_options",
    "get_engine",
    "get_session",
    "install_alembic_compat",
    "reset_engine",
]
