# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""SQLAlchemy declarative base for the CivicCast schema.

Per ADR 0008 — sync SQLAlchemy 2.0, typed ``DeclarativeBase`` with
``MetaData(schema="civiccast")`` so every future model lands in the
project's reserved Postgres namespace (CLAUDE.md closed decision:
"Schema: civiccast.* PostgreSQL namespace"). SQLite test fixtures receive
the schema via an ATTACH DATABASE hook registered below; Postgres enforces
the namespace at DDL time (the real-Postgres tests prove this).

Public surface re-exported from ``civiccast.db``.
"""

from __future__ import annotations

from sqlalchemy import MetaData, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

# Single shared MetaData instance keyed to the civiccast schema. Every
# model that inherits from Base inherits this MetaData, which means every
# table lands in the civiccast schema by default — the schema-namespace
# contract codified in ADR 0008.
_metadata = MetaData(schema="civiccast")


class Base(DeclarativeBase):
    """Project-wide declarative base.

    Inherit from this for every ORM model. The shared :data:`_metadata`
    binds the model into the ``civiccast`` Postgres schema (enforced on
    Postgres; emulated via ATTACH on SQLite test connections).
    """

    metadata = _metadata


@event.listens_for(Engine, "connect")
def _sqlite_attach_civiccast_schema(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    """ATTACH an in-memory DB as ``civiccast`` on SQLite connections.

    SQLite treats schema-qualified names (e.g. ``civiccast.alert_rules``)
    as attached-database references. Without this hook, ``create_all`` on a
    fresh in-memory engine fails with "unknown database civiccast". Postgres
    connections ignore this hook entirely (detected via DBAPI module name).
    Registered here (not in a specific model module) so any test that
    imports ``civiccast.db.Base`` gets the listener automatically.
    """
    if not type(dbapi_connection).__module__.startswith("sqlite3"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("ATTACH DATABASE ':memory:' AS civiccast")
    except Exception:  # noqa: S110 — already-attached is the only expected failure
        pass
    finally:
        cursor.close()
