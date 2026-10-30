# SPDX-License-Identifier: Apache-2.0
"""Preserve the old service profile before switching Windows service identity.

The installer starts the stopped service once in migration-only mode, under its
old account. That process can read its own Credential Manager entries. It never
starts station writers, and leaves the original profile and keyring intact.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from civiccast.native.service_identity import restrict_data_directory
from civiccast.native.station_secrets import StationSecretVault

MIGRATION_ENV = "CIVICCAST_IDENTITY_MIGRATION"


def station_profile_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CivicCast" / "profile"


def _files(root: Path) -> dict[Path, str]:
    """Reject redirected content; compare bytes before committing the copy."""
    if any(path.is_symlink() or path.is_junction() for path in (root, *root.parents)):
        raise RuntimeError("Station profile migration refuses redirected paths.")
    result = {}
    for path in root.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise RuntimeError("Station profile migration refuses redirected paths.")
        if path.is_file():
            with path.open("rb") as stream:
                result[path.relative_to(root)] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def preserve_profile(source: Path, destination: Path) -> None:
    """Copy once, verify, then atomically promote; never overwrite newer state."""
    if not source.exists():
        return
    if any(path.is_symlink() or path.is_junction() for path in (destination, *destination.parents)):
        raise RuntimeError("Station profile migration refuses redirected destination paths.")
    if destination.exists():
        if _files(source) != _files(destination):
            raise RuntimeError(
                "Both old and new station profiles exist and differ; migration stopped."
            )
        return
    expected = _files(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".profile-migration-", dir=destination.parent))
    try:
        restrict_data_directory(staging)
        shutil.copytree(source, staging, dirs_exist_ok=True)
        if _files(staging) != expected or _files(source) != expected:
            raise RuntimeError("Station profile changed during migration; original retained.")
        staging.replace(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def migrate_old_account() -> None:
    """Called ONLY inside the existing service, before starting any children."""
    from civiccast.certs.authority import _current_process_sid, _restrict_windows_private_key_acl
    from civiccast.native.service_identity import SERVICE_SID

    if _current_process_sid() not in {"S-1-5-18", SERVICE_SID}:
        raise RuntimeError("Station identity migration requires the installed service account.")
    profile = station_profile_root()
    source = Path(os.environ["LOCALAPPDATA"]) / "CivicCast"
    destination = profile / "CivicCast"
    if source.resolve() != destination.resolve():
        preserve_profile(source, destination)
    old_config = Path.home() / ".civiccast"
    new_config = profile / ".civiccast"
    if old_config.resolve() != new_config.resolve():
        preserve_profile(old_config, new_config)
    StationSecretVault().import_current_windows_keyring()
    receipt = profile / "identity-migration.complete"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("Profile copied and station credentials preserved.\n", encoding="utf-8")
    _restrict_windows_private_key_acl(receipt)
