# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bridges the installer-persisted ``DatabaseUrl`` registry value into the
service process's environment, BEFORE the production dependency provider
(``service.default_dependency_provider``) runs (beta BLOCKER #48).

The defect this fixes: the installer provisioning step (D4) writes the
resolved database credential to the product-owned registry value
``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl`` (written by the Rust
installer, ``native_service_registration.rs``'s ``write_database_url`` --
see that module's ``DATABASE_URL_KEY``/``DATABASE_URL_VALUE_NAME``, which
this module's constants below are pinned equal to by
``tests/native/test_service_env.py``). Nothing previously read that value
back into the SCM-hosted service process's environment, so
``default_dependency_provider``'s ``os.environ.get("DATABASE_URL", "")``
check always found it empty and raised, crashing the service on every real
install (Sandbox gauntlet run 11). ``service_host.SvcDoRun`` now calls
:func:`ensure_database_url_env` early, before ``_service_factory`` runs.

House lazy-import rule (matches ``civiccast.native.win_probes``): this module
imports only stdlib at module load, so ``import
civiccast.native.supervisor.service_env`` succeeds on Linux for the pure
unit suite (``tests/native/test_service_env.py``, an injectable fake
registry reader, no real winreg). ``winreg`` is imported lazily inside
:func:`read_database_url_from_registry`, the one function that touches it;
the real winreg round-trip (against a TEMPORARY HKCU key, never HKLM) is
proven in ``tests/native/test_service_env_win.py``.

Security notes:
* An already-set ``DATABASE_URL`` environment variable always WINS -- the
  registry is never even consulted in that case. This is the documented
  operator/test override path (see the error message below and
  ``default_dependency_provider``'s own docstring).
* The database URL VALUE is never logged, formatted into an exception
  message, or otherwise embedded anywhere by this module -- the Windows
  Event Log is readable by all local users, and the URL carries the DB
  password. The fail-loud error below names only the registry PATH and the
  environment variable NAME, never the value.
* Task #55 (audit-lite FINDING-004): a stale machine-level ``DATABASE_URL``
  env var (left over from a manual debug session, an old test harness, or an
  incomplete uninstall) silently wins over the registry's current, correct
  value on every SCM service start -- previously with zero trace in any log.
  :func:`ensure_database_url_env` now logs exactly ONE line naming which
  source won (``"environment override"`` vs ``"registry"``) -- the env var
  NAME / registry PATH, same as the fail-loud error above, NEVER the
  resolved value -- so a support engineer diagnosing "why is the service
  talking to the wrong database" does not have to guess.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    import winreg as _winreg_types

    _RegKeyType = _winreg_types.HKEYType | int
else:
    _RegKeyType = int

# Mirrors civiccast/apps/installer/src-tauri/src/native_service_registration.rs's
# `DATABASE_URL_KEY` / `DATABASE_URL_VALUE_NAME` -- that Rust module (via its
# `write_database_url`, invoked by the installer's D4 provisioning step) is
# the WRITER and source of truth for this registry location; no Python code
# writes it (see civiccast/native/provision/models.py's module docstring: "the
# registry write itself is the installer's ... per the WS5 task boundary").
# Pinned byte-for-byte equal to the Rust constants by
# tests/native/test_service_env.py::test_registry_constants_pinned_to_rust_writer.
DATABASE_URL_REGISTRY_KEY = r"SOFTWARE\CivicCast\Native"
DATABASE_URL_REGISTRY_VALUE_NAME = "DatabaseUrl"

# The environment variable default_dependency_provider (service.py) reads.
DATABASE_URL_ENV_VAR = "DATABASE_URL"

RegistryReader = Callable[[], str | None]


class DatabaseUrlUnavailableError(RuntimeError):
    """Raised when ``DATABASE_URL`` is unset/empty AND the installer-persisted
    registry value is also missing or empty -- there is no source left to bind
    the alerting Session to. Carries a precise, actionable message naming BOTH
    the registry path and the environment variable, NEVER a URL value."""


def read_database_url_from_registry(
    *, root: _RegKeyType | None = None, key_path: str = DATABASE_URL_REGISTRY_KEY
) -> str | None:
    """Read ``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl`` (or an injected
    ``root``/``key_path`` -- the win-only test points this at a temporary HKCU
    key). Returns ``None`` when the key or value is absent/blank; ``winreg`` is
    imported lazily here only, per the house cross-platform-import rule.
    """

    import winreg

    resolved_root = winreg.HKEY_LOCAL_MACHINE if root is None else root
    try:
        with winreg.OpenKey(
            resolved_root, key_path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as key:
            try:
                raw_value, _value_type = winreg.QueryValueEx(key, DATABASE_URL_REGISTRY_VALUE_NAME)
            except FileNotFoundError:
                return None
    except FileNotFoundError:
        return None

    text = str(raw_value).strip()
    return text or None


def ensure_database_url_env(
    *, registry_reader: RegistryReader = read_database_url_from_registry
) -> None:
    """Ensure ``os.environ["DATABASE_URL"]`` is populated before the production
    dependency provider runs.

    * If ``DATABASE_URL`` is already set (non-blank) in the environment, it
      WINS -- ``registry_reader`` is never called and the environment is left
      untouched. This is the documented operator/test override.
    * Else, ``registry_reader`` is consulted (the real one reads the
      installer-persisted HKLM registry value). If it returns a non-blank
      value, that value is written to ``os.environ["DATABASE_URL"]``.
    * Else (both sources empty/missing), raises
      :class:`DatabaseUrlUnavailableError` naming the exact registry path and
      the environment variable -- never the (absent) value -- so the Windows
      Event Log shows precisely what to fix.

    Task #55: logs exactly one line naming WHICH source won -- never the
    resolved value itself (see the module docstring's security note).
    """

    if os.environ.get(DATABASE_URL_ENV_VAR, "").strip():
        _LOG.info(
            "%s source: environment override (%s was already set; the registry "
            "value was not consulted)",
            DATABASE_URL_ENV_VAR,
            DATABASE_URL_ENV_VAR,
        )
        return  # operator/test override wins; registry not consulted.

    value = registry_reader()
    if value:
        os.environ[DATABASE_URL_ENV_VAR] = value
        _LOG.info(
            "%s source: registry (HKLM\\%s\\%s)",
            DATABASE_URL_ENV_VAR,
            DATABASE_URL_REGISTRY_KEY,
            DATABASE_URL_REGISTRY_VALUE_NAME,
        )
        return

    raise DatabaseUrlUnavailableError(
        f"{DATABASE_URL_ENV_VAR} is unset or empty, and the installer-persisted "
        f"registry value HKLM\\{DATABASE_URL_REGISTRY_KEY}\\{DATABASE_URL_REGISTRY_VALUE_NAME} "
        "is also missing or empty; the production supervisor dependency provider "
        f"needs a database URL to bind the alerting Session. Either set "
        f"{DATABASE_URL_ENV_VAR} in the service environment, or repair the "
        "installation so that registry value is populated, before running under "
        "the SCM."
    )


__all__ = [
    "DATABASE_URL_ENV_VAR",
    "DATABASE_URL_REGISTRY_KEY",
    "DATABASE_URL_REGISTRY_VALUE_NAME",
    "DatabaseUrlUnavailableError",
    "ensure_database_url_env",
    "read_database_url_from_registry",
]
