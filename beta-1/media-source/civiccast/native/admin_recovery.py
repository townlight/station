# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Offline first-admin recovery; deliberately has no HTTP entry point."""

from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import typer

from civiccast.installer.station_state import _replace_admin_password, _restrict_state_file
from civiccast.native.service_identity import is_station_service_account

admin_app = typer.Typer(
    name="admin", help="Local Windows administrator recovery.", no_args_is_help=True
)


class RecoveryError(RuntimeError):
    """A safe, credential-free diagnostic for the local operator."""


def _require_elevation() -> None:
    if sys.platform != "win32":
        raise RecoveryError("Administrator recovery is supported only on the Windows station.")
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        elevated = win32security.GetTokenInformation(token, win32security.TokenElevation)
        admin_sid = win32security.CreateWellKnownSid(
            win32security.WinBuiltinAdministratorsSid, None
        )
        administrator = win32security.CheckTokenMembership(None, admin_sid)
    finally:
        token.Close()
    if not elevated or not administrator:
        raise RecoveryError("Open PowerShell with Run as administrator, then retry.")


def _installed_state_path() -> Path:
    """Read the service identity/environment, never the invoking user's profile."""
    import winreg

    import win32api
    import win32service  # type: ignore[import-untyped]  # pywin32 runtime has no bundled stubs.

    from civiccast.native.supervisor.config import SERVICE_NAME

    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        service = win32service.OpenService(
            scm, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_QUERY_CONFIG
        )
        try:
            if win32service.QueryServiceStatus(service)[1] != win32service.SERVICE_STOPPED:
                raise RecoveryError(
                    "Stop CivicCastSupervisor before resetting the administrator password."
                )
            account = win32service.QueryServiceConfig(service)[7]
            if not is_station_service_account(account):
                raise RecoveryError(
                    "Recovery requires the installed CivicCast supervisor service identity."
                )
        finally:
            win32service.CloseServiceHandle(service)
    finally:
        win32service.CloseServiceHandle(scm)

    configured = None
    local_appdata = None
    machine_key = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, machine_key) as key:
        try:
            configured, kind = winreg.QueryValueEx(key, "CIVICCAST_STATION_STATE_PATH")
            if kind != winreg.REG_SZ:
                raise RecoveryError("Station-state override must be an absolute REG_SZ path.")
        except FileNotFoundError:
            pass
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
    ) as key:
        try:
            entries, kind = winreg.QueryValueEx(key, "Environment")
            if kind != winreg.REG_MULTI_SZ:
                raise RecoveryError("The service Environment registry value is invalid.")
            for entry in entries:
                name, separator, value = entry.partition("=")
                if separator and name.casefold() == "civiccast_station_state_path":
                    configured = value
                elif separator and name.casefold() == "localappdata":
                    local_appdata = value
        except FileNotFoundError:
            pass
    path = (
        Path(configured)
        if configured
        else (
            Path(local_appdata)
            if local_appdata
            else Path(win32api.GetWindowsDirectory())
            / "System32/config/systemprofile/AppData/Local"
        )
        / "CivicCast/station-state.json"
    )
    if not path.is_absolute() or str(path).startswith("\\\\") or "%" in str(path):
        raise RecoveryError(
            "Station state must use an absolute local path without environment substitutions."
        )
    # Refuse redirected files/directories rather than resetting a different station.
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise RecoveryError(
            "Station-state paths containing links or junctions are not supported for recovery."
        )
    return path


@contextmanager
def _offline_station() -> Iterator[Path]:
    _require_elevation()
    from civiccast.native.supervisor.service import build_singleton_mutex

    mutex = build_singleton_mutex()
    acquired = mutex.acquire()
    if acquired.status not in ("acquired", "acquired_abandoned"):
        raise RecoveryError(
            "The supervisor or another recovery command is active; stop it and retry."
        )
    try:
        # Holding the supervisor's own singleton prevents a start during the prompt/write.
        yield _installed_state_path()
    finally:
        mutex.release()


def _prompt_password() -> str:
    if not sys.stdin.isatty():
        raise RecoveryError(
            "Use an interactive terminal; passwords cannot be supplied by pipe or argument."
        )
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            password = getpass.getpass("New administrator password (12-256 characters): ")
            confirmation = getpass.getpass("Confirm new administrator password: ")
        except getpass.GetPassWarning:
            raise RecoveryError(
                "This terminal cannot hide password input; use an elevated PowerShell console."
            ) from None
    if password != confirmation:
        raise RecoveryError("Passwords did not match; nothing was changed.")
    return password


def _protected_copy(path: Path, data: bytes, *, suffix: str) -> Path:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=suffix, dir=path.parent)
    target = Path(name)
    try:
        # Harden the empty file BEFORE writing any password/session hashes.
        with os.fdopen(fd, "wb") as stream:
            _restrict_state_file(target, service_can_replace=suffix == ".tmp")
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def reset_password(path: Path, password: str) -> Path:
    """Credential-only mutation; caller must hold the offline station guard."""
    if not 12 <= len(password) <= 256:
        raise RecoveryError("Password must contain 12-256 characters; nothing was changed.")
    original = path.read_bytes()
    try:
        raw = json.loads(original)
    except (ValueError, UnicodeError):
        raise RecoveryError(
            "Station state is not valid JSON; restore a known-good backup instead."
        ) from None
    if (
        not isinstance(raw, dict)
        or raw.get("setup_complete") is not True
        or not isinstance(raw.get("station"), dict)
        or not isinstance(raw.get("admin"), dict)
        or not isinstance(raw["admin"].get("username"), str)
        or not isinstance(raw.get("operator_console"), dict)
        or not isinstance(raw.get("recovery"), dict)
    ):
        raise RecoveryError(
            "Completed station administrator state is missing; nothing was changed."
        )
    _replace_admin_password(raw, password)
    now = datetime.now(UTC).isoformat()
    raw["operator_console"] = {"tokens": [], "rotated_at": now}
    raw["recovery"]["code_hashes"] = []
    raw["recovery"]["local_admin_reset_at"] = now
    replacement = json.dumps(raw, indent=2, sort_keys=True).encode("utf-8")
    backup = _protected_copy(path, original, suffix=".before-admin-reset.bak")
    pending = _protected_copy(path, replacement, suffix=".tmp")
    try:
        # Refuse an out-of-band writer even though normal service starts are excluded.
        if path.read_bytes() != original:
            raise RecoveryError(
                "Station state changed during recovery; nothing was replaced. Stop all writers and retry."
            )
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return backup


@admin_app.command("reset-password")
def reset_password_command() -> None:
    """Recover first-admin access locally; revokes console sessions and recovery codes."""
    try:
        with _offline_station() as path:
            password = _prompt_password()
            backup = reset_password(path, password)
            username = json.loads(path.read_bytes())["admin"]["username"]
        typer.echo(f"Administrator password reset for {username}. Protected backup: {backup}")
        typer.echo(
            "Old console sessions and recovery codes were revoked. Start CivicCastSupervisor, sign in, then regenerate your recovery kit in Station Profile."
        )
    except RecoveryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    except Exception:
        # OS/ACL diagnostics can contain sensitive paths or state. Never dump locals.
        typer.echo(
            "Local recovery failed. Check service installation, state-file permissions and free disk space. The original state is preserved unless replacement already completed.",
            err=True,
        )
        raise typer.Exit(1) from None
