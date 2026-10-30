# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Stable Windows virtual-account identity; no password or administrator membership."""

from __future__ import annotations

from pathlib import Path

SERVICE_ACCOUNT = r"NT SERVICE\CivicCastSupervisor"
# Windows service SID: SHA-1 of the upper-case UTF-16LE service name, five LE DWORDs.
SERVICE_SID = "S-1-5-80-3505678282-1640658708-3289577692-12763909-1392209962"


def object_dacl() -> str:
    """Allow service coordination only to this service, SYSTEM and administrators."""
    return f"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{SERVICE_SID})"


def directory_dacl(*, writable: bool) -> str:
    """Runtime binaries are read/execute; mutable station data is modify-only."""
    service_rights = "0x1301bf" if writable else "0x1200a9"
    return f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;{service_rights};;;{SERVICE_SID})"


def is_station_service_account(account: str) -> bool:
    """Accept the legacy identity while an existing installation is being migrated."""
    return account.casefold() in {"localsystem", r"nt authority\system", SERVICE_ACCOUNT.casefold()}


def restrict_data_directory(path: Path, *, writable: bool = True) -> None:
    """Protect a newly created station-data directory before writing secrets."""
    import os

    if os.name != "nt":
        path.chmod(0o700)
        return
    import win32security

    from civiccast.certs.authority import _current_process_sid

    writer = _current_process_sid()
    sddl = directory_dacl(writable=writable)
    if writer not in {SERVICE_SID, "S-1-5-18"}:
        sddl += f"(A;OICI;FA;;;{writer})"
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    win32security.SetFileSecurity(
        str(path),
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )
