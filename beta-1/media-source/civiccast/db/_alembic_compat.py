# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Alembic compatibility shim — env-var resolution for ``sqlalchemy.url``.

Per ADR 0008, ``alembic.ini`` resolves ``sqlalchemy.url`` from the
``DATABASE_URL`` environment variable. Alembic's :class:`Config` uses
:class:`configparser.ConfigParser` with ``BasicInterpolation``, which
substitutes ``%(KEY)s`` references against the section's own keys plus
the ``[DEFAULT]`` section but does NOT pull from ``os.environ`` and does
NOT support a fallback from env vars natively.

This module exposes :func:`install` — a one-time idempotent patch on
:class:`alembic.config.Config` that fills ``sqlalchemy.url`` from
``DATABASE_URL`` on every ``Config.__init__`` when the option is missing
or empty. **The patch is no longer applied at import time.** Callers must
invoke :func:`install` explicitly. The two callers are:

- ``alembic/env.py`` (the project's root migration env) — calls
  :func:`install` before constructing or consuming any ``Config``.
- The test suite's :mod:`tests.db.conftest` — calls :func:`install` once
  at module load so test cases that build ``Config(str(ALEMBIC_INI))``
  directly observe the fill.

Moving from import-side-effect to explicit ``install()`` makes the patch
auditable: a reader of ``civiccast.db.__init__`` no longer has to know
that importing the package mutates a third-party class. The patch behaviour
is unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from alembic.config import Config

_PATCHED_ATTR = "_civiccast_database_url_resolver_patched"


def install() -> None:
    """Patch :class:`alembic.config.Config` to resolve ``sqlalchemy.url``
    from the ``DATABASE_URL`` env var when missing or empty.

    Idempotent — calling more than once is a no-op. Safe to invoke from
    test fixtures, conftest, and the alembic env.py.
    """
    if getattr(Config, _PATCHED_ATTR, False):
        return

    original_init = Config.__init__

    def patched_init(self: Config, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        env_url = os.environ.get("DATABASE_URL")
        if not env_url:
            return
        # Fill sqlalchemy.url only if the ini left it empty/unset. An
        # explicit value in alembic.ini wins over the env var.
        existing = self.get_main_option("sqlalchemy.url")
        if not existing:
            self.set_main_option("sqlalchemy.url", env_url)

    Config.__init__ = patched_init  # type: ignore[method-assign]
    setattr(Config, _PATCHED_ATTR, True)
