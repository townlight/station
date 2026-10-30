# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Alembic environment for the CivicCast monorepo.

Per ADR 0008 + director-decisions §2 of run 2026-05-08-db-foundation:
one env.py at the repo root, per-module migration directories under
``civiccast/<module>/migrations/versions/``. This module discovers
those directories at startup and feeds the assembled list to Alembic's
``version_locations`` so a single ``alembic upgrade head`` walks every
module's migration graph.

The reserved repo-root ``alembic/versions/`` slot is included for
completeness — convention is to keep migrations in their owning
module's directory, but a migration accidentally dropped at the
repo-root slot is still picked up rather than silently ignored.

The discovery root is resolved relative to *this file's* location, not
the current working directory. This matters because CI matrix jobs
sometimes invoke Alembic from a non-repo-root cwd (a real failure
mode); a cwd-relative discovery would silently return zero versions
and turn ``alembic upgrade head`` into a no-op for every module.

The migration runner code (``run_migrations_offline``,
``run_migrations_online``, and the dispatch at the bottom) only runs
when this module is loaded *by Alembic* — i.e. when
``alembic.context`` has been initialised with a live ``EnvironmentContext``.
Direct imports (e.g. unit tests calling :func:`discover_version_locations`)
load the discovery helpers without touching the runtime context, so
they do not require an active Alembic invocation.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from civiccast.db import Base, connect_options, install_alembic_compat

# Resolve sqlalchemy.url from DATABASE_URL when alembic.ini leaves it empty.
# Per ADR 0008. Idempotent — safe to call here even when tests have already
# called it via tests.db.conftest.
install_alembic_compat()

# Repo root is two directories up from this file when running from the source
# tree (alembic/env.py -> repo). In the packaged wheel, this same env.py is
# copied under civiccast/alembic/env.py, so the package root is already the
# civiccast module directory.
_ENV_DIR = Path(__file__).resolve().parent
_SOURCE_ROOT = _ENV_DIR.parent
if (_SOURCE_ROOT / "civiccast").is_dir():
    _REPO_ROOT = _SOURCE_ROOT
    _CIVICCAST_ROOT = _REPO_ROOT / "civiccast"
else:
    _REPO_ROOT = _SOURCE_ROOT.parent
    _CIVICCAST_ROOT = _SOURCE_ROOT
_RESERVED_SLOT = _ENV_DIR / "versions"

# The MetaData every model registers against. Empty in this rung — the
# first real model + migration land in task 1b.
target_metadata = Base.metadata


def discover_version_locations(root: Path) -> list[str]:
    """Return per-module Alembic version directories under ``root``.

    Walks ``root`` (typically the ``civiccast/`` package) for
    ``<module>/migrations/versions/`` directories. Directories with a
    ``migrations/`` but no ``versions/`` subdirectory (half-built
    modules) are excluded so they do not break the runner.

    Returned paths are absolute strings, ordered for stable output.
    """
    if not root.exists():
        return []
    discovered: list[str] = []
    for migrations_dir in sorted(root.glob("*/migrations")):
        versions_dir = migrations_dir / "versions"
        if versions_dir.is_dir():
            discovered.append(str(versions_dir.resolve()))
    return discovered


def all_version_locations() -> list[str]:
    """Return every version location Alembic should consider.

    The full set is:

    * The reserved repo-root ``alembic/versions/`` slot (always
      included even when empty).
    * Every ``civiccast/<module>/migrations/versions/`` discovered by
      :func:`discover_version_locations`.
    """
    locations = [str(_RESERVED_SLOT.resolve())]
    locations.extend(discover_version_locations(_CIVICCAST_ROOT))
    return locations


def run_migrations_offline() -> None:
    """Run migrations without a live DBAPI connection.

    Generates SQL to stdout instead of executing against a database.
    Useful for review or for environments where the migration runner
    cannot reach the DB directly.
    """
    config = context.config
    locations = all_version_locations()
    url = config.get_main_option("sqlalchemy.url")
    # SQLite ignores schema qualifiers; suppress the civiccast schema
    # binding when the dialect cannot enforce it. Postgres and any
    # other dialect that supports schemas keeps the namespace.
    use_schema = url is not None and not url.startswith("sqlite")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema="civiccast" if use_schema else None,
        include_schemas=use_schema,
        version_locations=locations,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database.

    Builds an :class:`Engine` from the alembic.ini section and runs the
    migration graph in a transaction.
    """
    config = context.config
    locations = all_version_locations()
    section = config.get_section(config.config_ini_section, {}) or {}
    # Bound the connect attempt so `alembic upgrade head` against an unreachable
    # Postgres fails fast instead of hanging for the driver's own default
    # (PE-2026-07-22-1). engine_from_config forwards connect_args to create_engine;
    # connect_options() is a no-op for sqlite.
    migration_url = config.get_main_option("sqlalchemy.url") or section.get("sqlalchemy.url", "")
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        **connect_options(migration_url or ""),
    )

    try:
        with connectable.connect() as connection:
            # SQLite ignores schema qualifiers; suppress the civiccast schema
            # binding when the dialect cannot enforce it.
            use_schema = connection.dialect.name != "sqlite"
            if use_schema:
                # Ensure the civiccast schema exists before Alembic creates
                # its version table (which lives in the same schema per
                # version_table_schema kwarg below). Idempotent. Required on
                # a fresh Postgres database where the schema has not yet
                # been provisioned by an operator.
                from sqlalchemy import text as _sql_text

                connection.execute(_sql_text("CREATE SCHEMA IF NOT EXISTS civiccast"))
                # Alembic's default ``alembic_version.version_num`` column is
                # VARCHAR(32). Our descriptive revision slugs exceed 32 chars
                # (e.g. ``0042_takeover_audit_and_command_action`` = 38), which
                # SQLite silently tolerates but Postgres rejects with
                # StringDataRightTruncation the moment such a revision is
                # stamped. Pre-create the version table wide, and widen it if a
                # partially-migrated database already has the 32-char column, so
                # the chain can stamp any slug. Idempotent.
                connection.execute(
                    _sql_text(
                        "CREATE TABLE IF NOT EXISTS civiccast.alembic_version ("
                        "version_num VARCHAR(255) NOT NULL, "
                        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                    )
                )
                connection.execute(
                    _sql_text(
                        "ALTER TABLE civiccast.alembic_version "
                        "ALTER COLUMN version_num TYPE VARCHAR(255)"
                    )
                )
                connection.commit()
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                version_table_schema="civiccast" if use_schema else None,
                include_schemas=use_schema,
                version_locations=locations,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


def _is_running_under_alembic() -> bool:
    """Return True if loaded inside a live Alembic ``EnvironmentContext``.

    When the module is imported directly (for example by a unit test
    using ``importlib.util.spec_from_file_location``), Alembic's
    ``context`` proxy has no live config and accessing ``context.config``
    raises :class:`AttributeError`. The discovery helpers above do not
    depend on the runtime context, so direct imports succeed; the
    migration dispatch below is gated on the runtime context being live.
    """
    try:
        # Touching context.config raises AttributeError when not bound.
        _ = context.config
    except (AttributeError, NameError):
        return False
    return True


if _is_running_under_alembic():
    config = context.config

    # Set up Python logging per alembic.ini (idempotent if already configured).
    if config.config_file_name is not None:
        fileConfig(config.config_file_name, disable_existing_loggers=False)

    # Resolve sqlalchemy.url from DATABASE_URL if interpolation did not
    # already pick it up. Tests use Config.set_main_option directly so
    # they bypass interpolation entirely.
    if not config.get_main_option("sqlalchemy.url"):
        env_url = os.environ.get("DATABASE_URL")
        if env_url:
            config.set_main_option("sqlalchemy.url", env_url)

    # Tell Alembic about every per-module version directory we found.
    # Newline-separated to survive paths that contain spaces (Windows
    # OneDrive paths, mixed-case macOS bundles); paired with
    # ``path_separator = newline`` in alembic.ini.
    config.set_main_option("version_locations", "\n".join(all_version_locations()))

    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
