# SPDX-License-Identifier: Apache-2.0
"""Installer-only transition from LocalSystem to the station virtual account."""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from contextlib import suppress
from pathlib import Path

from civiccast.native.identity_migration import MIGRATION_ENV, station_profile_root
from civiccast.native.service_identity import (
    SERVICE_ACCOUNT,
    SERVICE_SID,
    is_station_service_account,
    restrict_data_directory,
)

SERVICE_NAME = "CivicCastSupervisor"
SERVICE_KEY = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"


def _configured_storage_paths(entries: list[str], state_path: Path) -> list[Path]:
    values = {name.upper(): value for entry in entries for name, _, value in [entry.partition("=")]}
    station = (
        json.loads(state_path.read_text(encoding="utf-8")).get("station", {})
        if state_path.is_file()
        else {}
    )
    storage = station.get("storage_locations", {})
    paths = []
    for field, env in (
        ("media_library", "CIVICCAST_STATION_MEDIA_LIBRARY"),
        ("recordings", "CIVICCAST_STATION_RECORDINGS"),
        ("backups", "CIVICCAST_STATION_BACKUPS"),
    ):
        value = values.get(env) or os.environ.get(env) or storage.get(field)
        if value:
            path = Path(value)
            if not path.is_absolute() or path == Path(path.anchor):
                raise RuntimeError(
                    "Station storage must name an absolute dedicated folder, not a drive root."
                )
            paths.append(path)
    return paths


def _grant_database_key_read() -> None:
    """Repair/re-registration must work even when provisioning did not rewrite this key."""
    _grant_secret_registry_read(r"SOFTWARE\CivicCast\Native")


def _grant_service_environment_read() -> None:
    """Service Environment may include configured provider/SMTP credentials."""
    _grant_secret_registry_read(SERVICE_KEY)


def _grant_secret_registry_read(key_path: str) -> None:
    import winreg

    import win32con
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        # PythonClass/PythonPath subkeys must remain readable on later starts.
        f"D:P(A;CI;KA;;;SY)(A;CI;KA;;;BA)(A;CI;KR;;;{SERVICE_SID})",
        1,
    )
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        key_path,
        0,
        winreg.KEY_READ | win32con.WRITE_DAC | winreg.KEY_WOW64_64KEY,
    ) as key:
        win32security.SetSecurityInfo(
            int(key),
            win32security.SE_REGISTRY_KEY,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            descriptor.GetSecurityDescriptorDacl(),
            None,
        )


def _grant_tree(path: Path, rights: str, *, recursive: bool = True) -> None:
    if any(_is_reparse_point(part) for part in (path, *path.parents)):
        raise RuntimeError("Service permission migration refuses redirected paths.")
    if not path.exists():
        return
    _grant_tree_native_sid(
        _grantable_members(path, recursive=recursive),
        rights,
        propagate_to_children=not recursive and path.is_dir(),
    )


def _is_reparse_point(path: Path) -> bool:
    """Return whether a path is a link or another Windows reparse point."""

    try:
        attributes = path.lstat().st_file_attributes
    except FileNotFoundError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _grantable_members(path: Path, *, recursive: bool) -> list[Path]:
    """Collect ordinary descendants without following or granting reparse points."""

    members = [path]
    if not recursive or not path.is_dir():
        return members

    pending = [path]
    reparse_attribute = stat.FILE_ATTRIBUTE_REPARSE_POINT
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name.casefold()):
                child_info = entry.stat(follow_symlinks=False)
                if child_info.st_file_attributes & reparse_attribute:
                    continue
                child = Path(entry.path)
                members.append(child)
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)
    return members


def _grant_tree_native_sid(
    members: list[Path], rights: str, *, propagate_to_children: bool = False
) -> None:
    """Apply the service SID to prevalidated handles with scoped propagation.

    Existing explicit grants for this SID are replaced; all other ACL entries
    and each object's protected-DACL state are retained. Handles are opened
    without following a final reparse point. Recursive directory handles use
    MAXIMUM_ALLOWED to prevent automatic propagation; file handles request
    only the ACL rights needed to avoid sharing conflicts with running images.
    Nonrecursive directory grants retain normal Windows inheritance to existing
    unprotected children.
    """

    import win32con
    import win32file
    import win32security

    masks = {"M": 0x001301BF, "RX": 0x001200A9}
    try:
        access_mask = masks[rights]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported restricted service access right: {rights}") from exc

    service_sid = win32security.ConvertStringSidToSid(SERVICE_SID)
    for member in members:
        if any(_is_reparse_point(part) for part in (member, *member.parents)):
            # Recheck the path and ancestors before opening the final component.
            continue
        attributes = member.lstat().st_file_attributes
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            continue
        is_directory = bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
        desired_access = (
            win32con.MAXIMUM_ALLOWED
            if is_directory and not propagate_to_children
            else win32con.READ_CONTROL | win32con.WRITE_DAC
        )
        handle = win32file.CreateFile(
            str(member),
            desired_access,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_BACKUP_SEMANTICS | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        try:
            info = win32file.GetFileInformationByHandle(handle)
            if (
                info[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or bool(info[0] & stat.FILE_ATTRIBUTE_DIRECTORY) != is_directory
            ):
                continue
            descriptor = win32security.GetSecurityInfo(
                handle,
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
            )
            acl = descriptor.GetSecurityDescriptorDacl()
            if acl is None:
                raise RuntimeError(f"Cannot add restricted service access to a NULL DACL: {member}")
            for index in range(acl.GetAceCount() - 1, -1, -1):
                ace = acl.GetAce(index)
                header = ace[0]
                if not isinstance(header, tuple) or len(header) < 2:
                    continue
                ace_type, ace_flags = header[:2]
                if (
                    ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE
                    and not ace_flags & win32security.INHERITED_ACE
                    and len(ace) == 3
                    and win32security.ConvertSidToStringSid(ace[2]) == SERVICE_SID
                ):
                    acl.DeleteAce(index)

            inheritance_flags = (
                win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
                if info[0] & stat.FILE_ATTRIBUTE_DIRECTORY
                else 0
            )
            acl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS, inheritance_flags, access_mask, service_sid
            )
            control, _ = descriptor.GetSecurityDescriptorControl()
            protection = (
                win32security.PROTECTED_DACL_SECURITY_INFORMATION
                if control & win32security.SE_DACL_PROTECTED
                else win32security.UNPROTECTED_DACL_SECURITY_INFORMATION
            )
            win32security.SetSecurityInfo(
                handle,
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION | protection,
                None,
                None,
                acl,
                None,
            )
        finally:
            handle.Close()


def configure_service_identity(install_root: Path) -> None:
    """Run elevated after registration, before any normal service startup.

    On failure, leave the former account and service environment configured.
    Added ACL entries are restricted to the product's own virtual service SID.
    """
    import winreg

    import win32service  # type: ignore[import-untyped]  # pywin32 has no bundled stubs

    from civiccast.native.admin_recovery import _require_elevation

    _require_elevation()
    root = install_root.resolve(strict=True)
    if not (root / "runtime" / "python.exe").is_file():
        raise RuntimeError("Service identity migration requires the installed runtime root.")
    data_root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CivicCast"
    if root == Path(root.anchor) or root == data_root or root in data_root.parents:
        raise RuntimeError(
            "Application files and mutable station data require separate directories."
        )
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    service = None
    try:
        service = win32service.OpenService(
            scm,
            SERVICE_NAME,
            win32service.SERVICE_QUERY_CONFIG
            | win32service.SERVICE_QUERY_STATUS
            | win32service.SERVICE_CHANGE_CONFIG
            | win32service.SERVICE_START
            | win32service.SERVICE_STOP,
        )
        config = win32service.QueryServiceConfig(service)
        account = config[7]
        if not is_station_service_account(account):
            raise RuntimeError(
                "Custom service accounts require an explicit migration; installation was not changed."
            )
        if win32service.QueryServiceStatus(service)[1] != win32service.SERVICE_STOPPED:
            raise RuntimeError("Stop the station before changing its service identity.")
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, SERVICE_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as key:
            try:
                original, kind = winreg.QueryValueEx(key, "Environment")
                if kind != winreg.REG_MULTI_SZ:
                    raise RuntimeError("Invalid existing service Environment value.")
            except FileNotFoundError:
                original = None
            entries = list(original or [])
            retain_migration_mode = False
            try:
                if account.casefold() != SERVICE_ACCOUNT.casefold():
                    receipt = station_profile_root() / "identity-migration.complete"
                    receipt.unlink(missing_ok=True)
                    migration_env = [
                        e for e in entries if not e.upper().startswith(MIGRATION_ENV + "=")
                    ]
                    winreg.SetValueEx(
                        key,
                        "Environment",
                        0,
                        winreg.REG_MULTI_SZ,
                        [*migration_env, MIGRATION_ENV + "=1"],
                    )
                    win32service.StartService(service, None)
                    deadline = time.monotonic() + 90
                    while (
                        win32service.QueryServiceStatus(service)[1] != win32service.SERVICE_STOPPED
                    ):
                        if time.monotonic() >= deadline:
                            win32service.ControlService(service, win32service.SERVICE_CONTROL_STOP)
                            stop_deadline = time.monotonic() + 30
                            while (
                                win32service.QueryServiceStatus(service)[1]
                                != win32service.SERVICE_STOPPED
                            ):
                                if time.monotonic() >= stop_deadline:
                                    retain_migration_mode = True
                                    raise RuntimeError(
                                        "Station profile migration is still stopping; migration-only "
                                        "mode retained. Do not restart the station until it stops."
                                    )
                                time.sleep(0.2)
                            raise RuntimeError(
                                "Station profile migration did not finish within 90 seconds."
                            )
                        time.sleep(0.2)
                    if not receipt.is_file():
                        raise RuntimeError(
                            "Old service could not preserve its credentials/profile; account unchanged."
                        )

                # Preserve protected installer/recovery directories: a blanket
                # recursive grant would explicitly bypass their protected DACLs.
                _grant_tree(data_root, "M", recursive=False)
                for directory in (
                    "data",
                    "logs",
                    "profile",
                    "credentials",
                    "config",
                    "packs",
                    "components",
                ):
                    _grant_tree(data_root / directory, "M")
                # Protect the runtime boundary even when it lives beneath the
                # data root; otherwise inherited Modify defeats an RX grant.
                restrict_data_directory(root, writable=False)
                _grant_tree(root, "RX")
                _grant_database_key_read()
                _grant_service_environment_read()
                # Explicit station-state overrides keep their location and contents.
                state_path = Path(
                    os.environ.get("CIVICCAST_STATION_STATE_PATH")
                    or station_profile_root() / "CivicCast" / "station-state.json"
                )
                for entry in entries:
                    name, _, value = entry.partition("=")
                    if name.upper() == "CIVICCAST_STATION_STATE_PATH":
                        state_path = Path(value)
                        if not state_path.is_absolute():
                            raise RuntimeError("Station state override must be absolute.")
                        _grant_tree(state_path.parent, "M", recursive=False)
                        _grant_tree(state_path, "M", recursive=False)

                for storage_path in _configured_storage_paths(entries, state_path):
                    if (
                        storage_path == root
                        or storage_path in root.parents
                        or root in storage_path.parents
                    ):
                        raise RuntimeError(
                            "Mutable media/backup storage must be separate from application files."
                        )
                    if any(
                        p.is_symlink() or p.is_junction()
                        for p in (storage_path, *storage_path.parents)
                    ):
                        raise RuntimeError("Station storage permissions refuse redirected paths.")
                    storage_path.mkdir(parents=True, exist_ok=True)
                    _grant_tree(storage_path, "M")

                configured = [
                    e
                    for e in entries
                    if e.partition("=")[0].upper()
                    not in {MIGRATION_ENV, "LOCALAPPDATA", "USERPROFILE"}
                ]
                configured.append(f"LOCALAPPDATA={station_profile_root()}")
                configured.append(f"USERPROFILE={station_profile_root()}")
                winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, configured)
                win32service.ChangeServiceConfig(
                    service,
                    win32service.SERVICE_NO_CHANGE,
                    win32service.SERVICE_NO_CHANGE,
                    win32service.SERVICE_NO_CHANGE,
                    None,
                    None,
                    0,
                    None,
                    SERVICE_ACCOUNT,
                    None,  # Virtual accounts require NULL, not an empty password.
                    None,
                )
            except Exception:
                if retain_migration_mode:
                    raise
                if original is None:
                    with suppress(FileNotFoundError):
                        winreg.DeleteValue(key, "Environment")
                else:
                    winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, original)
                raise
    finally:
        if service is not None:
            win32service.CloseServiceHandle(service)
        win32service.CloseServiceHandle(scm)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=Path, required=True)
    args = parser.parse_args()
    configure_service_identity(args.install_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
