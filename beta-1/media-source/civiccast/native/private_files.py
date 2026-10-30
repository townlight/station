# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Atomic file helpers for small local plaintext secret stores."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from civiccast.native.service_identity import SERVICE_SID

_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"
_SERVICE_FILE_REPLACE_MASK = 0x0013019F  # FILE_GENERIC_READ | FILE_GENERIC_WRITE | DELETE
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class PrivateFileProtectionError(RuntimeError):
    """Raised when a local secret file cannot be safely read or protected."""


@contextmanager
def private_file_lock(path: Path) -> Iterator[None]:
    """Serialize reads, migrations, and updates of one file in this process."""

    key = os.path.normcase(str(path.resolve()))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def write_private_bytes_atomic(path: Path, value: bytes) -> None:
    """Write bytes through an empty, protected sibling and atomically replace.

    On Windows the temporary file's DACL is protected before any bytes are
    written. On other platforms its owner-only mode is set before writing.
    A protection or replace failure leaves an existing destination untouched.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with private_file_lock(path):
        descriptor, name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name[:40]}-",
            suffix=".tmp",
        )
        temporary = Path(name)
        open_descriptor: int | None = descriptor
        try:
            _protect_empty_file(temporary, descriptor)
            with os.fdopen(descriptor, "wb") as stream:
                open_descriptor = None
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            if open_descriptor is not None:
                os.close(open_descriptor)
            temporary.unlink(missing_ok=True)


def read_private_bytes(path: Path) -> bytes:
    """Read a local secret file and migrate broad ACLs without changing bytes."""

    with private_file_lock(path):
        try:
            is_private = _has_private_permissions(path)
        except Exception as exc:
            raise PrivateFileProtectionError(
                f"Cannot verify access protection for local secret file {str(path)!r}."
            ) from exc

        try:
            value = path.read_bytes()
        except OSError as exc:
            raise PrivateFileProtectionError(
                f"Cannot read existing local secret file {str(path)!r}; it was not replaced."
            ) from exc
        if is_private:
            return value
        try:
            write_private_bytes_atomic(path, value)
        except Exception as exc:
            raise PrivateFileProtectionError(
                f"Cannot safely migrate local secret file {str(path)!r}; the original was retained."
            ) from exc
        return value


def _protect_empty_file(path: Path, descriptor: int) -> None:
    if os.name == "nt":
        try:
            _set_windows_private_file_acl(path)
            if not _has_private_permissions(path):
                raise RuntimeError("The protected DACL is missing required service file rights.")
        except Exception as exc:
            raise PrivateFileProtectionError(
                f"Cannot establish a private Windows ACL for local secret file {str(path)!r}."
            ) from exc
        return
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)  # type: ignore[attr-defined, unused-ignore]
    except OSError as exc:
        raise PrivateFileProtectionError(
            f"Cannot establish private permissions for local secret file {str(path)!r}."
        ) from exc


def _has_private_permissions(path: Path) -> bool:
    if os.name != "nt":
        return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0

    import win32security

    from civiccast.certs.authority import _current_process_sid

    descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
    control, _revision = descriptor.GetSecurityDescriptorControl()
    if not control & win32security.SE_DACL_PROTECTED:
        return False
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        return False

    allowed_sids = {
        _current_process_sid(),
        _SYSTEM_SID,
        _ADMINISTRATORS_SID,
        SERVICE_SID,
    }
    granted_sids: set[str] = set()
    service_allow_mask = 0
    service_denied_mask = 0
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        ace_type = ace[0][0]
        if ace_type == 1:  # ACCESS_DENIED_ACE_TYPE
            if win32security.ConvertSidToStringSid(ace[2]) == SERVICE_SID:
                service_denied_mask |= ace[1]
            continue
        if ace_type != 0:  # ACCESS_ALLOWED_ACE_TYPE
            return False
        sid = win32security.ConvertSidToStringSid(ace[2])
        if sid not in allowed_sids:
            return False
        granted_sids.add(sid)
        if sid == SERVICE_SID:
            service_allow_mask |= ace[1]

    service_replace_rights = 0x00010003  # FILE_READ_DATA | FILE_WRITE_DATA | DELETE
    return (
        {_SYSTEM_SID, _ADMINISTRATORS_SID, SERVICE_SID}.issubset(granted_sids)
        and service_allow_mask & service_replace_rights == service_replace_rights
        and service_denied_mask & service_replace_rights == 0
    )


def _set_windows_private_file_acl(path: Path) -> None:
    """Set a strict allowlist DACL on the empty temporary file.

    Do not edit the descriptor inherited from ``mkstemp``: explicit ACEs from
    the parent can remain even when inheritance is disabled. This replaces
    that DACL with the writer, SYSTEM, Administrators, and the service. The
    service receives file read/write/delete so it can replace the finished
    file later; if it is the writer, its full writer grant already covers
    those rights.
    """

    import win32security

    from civiccast.certs.authority import _current_process_sid

    writer_sid = _current_process_sid()
    grants = {
        writer_sid: "FA",
        _SYSTEM_SID: "FA",
        _ADMINISTRATORS_SID: "FA",
    }
    if SERVICE_SID not in grants:
        grants[SERVICE_SID] = f"0x{_SERVICE_FILE_REPLACE_MASK:08X}"
    sddl = "D:P" + "".join(f"(A;;{rights};;;{sid})" for sid, rights in grants.items())
    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
    win32security.SetFileSecurity(
        str(path),
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )
