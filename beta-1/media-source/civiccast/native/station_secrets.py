# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Portable station credential interface backed by a restricted machine-DPAPI vault.

Only CivicCast namespaces enter this backend. Export is deliberately an in-memory
operation for encrypted backup; the caller must never write its result in plaintext.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

_LOCK = threading.RLock()
NAMESPACES = frozenset(
    {
        "civiccast.staff-token",
        "civiccast.live-source",
        "civiccast.control-room-device",
        "civiccast.ai-provider",
    }
)


def vault_path() -> Path:
    return (
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "CivicCast"
        / "credentials"
        / "station-vault.dpapi"
    )


def _protect(value: bytes) -> bytes:
    import win32crypt

    return cast(
        bytes,
        win32crypt.CryptProtectData(value, "CivicCast station credentials", None, None, None, 4),
    )


def _unprotect(value: bytes) -> bytes:
    import win32crypt

    return cast(bytes, win32crypt.CryptUnprotectData(value, None, None, None, 0)[1])


def write_protected_secret(path: Path, value: bytes) -> None:
    from civiccast.certs.authority import _restrict_windows_private_key_acl
    from civiccast.native.service_identity import restrict_data_directory

    try:
        path.parent.mkdir(parents=True)
    except FileExistsError:
        if not path.parent.is_dir():
            raise
        # The installer owns the existing credentials directory. The station
        # service has Modify rights there, deliberately without WRITE_DAC.
        # Each secret file is still restricted below before atomic replacement.
    else:
        try:
            restrict_data_directory(path.parent)
        except Exception:
            # Leave no unprotected directory that a retry could reuse.
            path.parent.rmdir()
            raise
    encrypted = _protect(value)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".vault-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        _restrict_windows_private_key_acl(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class StationSecretVault:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or vault_path()

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(_unprotect(self.path.read_bytes()))
            if not isinstance(value, dict) or any(
                name not in NAMESPACES
                or not isinstance(entries, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in entries.items())
                for name, entries in value.items()
            ):
                raise ValueError("Invalid credential namespace or record")
            return value
        except Exception:
            raise RuntimeError(
                "Station credential vault cannot be opened; restore it from backup."
            ) from None

    def _write(self, value: dict[str, dict[str, str]]) -> None:
        write_protected_secret(self.path, json.dumps(value).encode("utf-8"))

    def get(self, namespace: str, username: str) -> str | None:
        with _LOCK:
            return self._read().get(namespace, {}).get(username)

    def set(self, namespace: str, username: str, password: str) -> None:
        if namespace not in NAMESPACES:
            raise ValueError("Not a CivicCast credential namespace")
        with _LOCK:
            value = self._read()
            value.setdefault(namespace, {})[username] = password
            self._write(value)

    def delete(self, namespace: str, username: str) -> None:
        with _LOCK:
            value = self._read()
            if username not in value.get(namespace, {}):
                raise PasswordDeleteError("Station credential does not exist")
            del value[namespace][username]
            self._write(value)

    def export_portable(self) -> Mapping[str, bytes]:
        with _LOCK:
            return {"station-keyring.json": json.dumps(self._read()).encode("utf-8")}

    def import_portable(self, records: Mapping[str, bytes]) -> None:
        raw = records.get("station-keyring.json")
        if raw is None:
            return
        value = json.loads(raw)
        if not isinstance(value, dict) or any(
            name not in NAMESPACES
            or not isinstance(entries, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in entries.items())
            for name, entries in value.items()
        ):
            raise ValueError("Backup contains invalid station credentials")
        with _LOCK:
            self._write(value)

    def import_current_windows_keyring(self) -> int:
        """Run in the OLD service account before changing identity; preserve originals."""
        import pywintypes
        import win32cred
        from keyring.backends.Windows import DecodingCredential

        try:
            credentials = win32cred.CredEnumerate(None, 0)
        except pywintypes.error as error:
            if error.winerror == 1168:
                credentials = []
            else:
                raise
        count = 0
        with _LOCK:
            # The old account is authoritative until cutover. Rebuild the snapshot
            # on retry so changed or deleted credentials cannot be resurrected.
            value: dict[str, dict[str, str]] = {}
            # Windows keyring prefers an exact service-name record over a
            # compound username@service record for the same account.
            credentials = sorted(
                credentials, key=lambda item: item.get("TargetName", "") in NAMESPACES
            )
            for credential in credentials:
                target = credential.get("TargetName", "")
                username = credential.get("UserName", "")
                namespace = next((n for n in NAMESPACES if target in (n, f"{username}@{n}")), None)
                if namespace is None or credential.get("Type") != win32cred.CRED_TYPE_GENERIC:
                    continue
                value.setdefault(namespace, {})[username] = DecodingCredential(credential).value
                count += 1
            self._write(value)
        return count


class StationKeyring(KeyringBackend):
    priority = 0

    def __init__(self, vault: StationSecretVault, fallback: KeyringBackend) -> None:
        self.vault = vault
        self.fallback = fallback

    def get_password(self, service: str, username: str) -> str | None:
        if service in NAMESPACES:
            return self.vault.get(service, username)
        return self.fallback.get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        if service in NAMESPACES:
            self.vault.set(service, username, password)
        else:
            self.fallback.set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        if service in NAMESPACES:
            self.vault.delete(service, username)
        else:
            self.fallback.delete_password(service, username)


def configure_station_keyring() -> None:
    if os.name != "nt":
        return
    import keyring

    current = keyring.get_keyring()
    if not isinstance(current, StationKeyring):
        keyring.set_keyring(StationKeyring(StationSecretVault(), current))
