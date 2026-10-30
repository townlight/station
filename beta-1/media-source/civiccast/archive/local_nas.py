# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real local-NAS archive transport (issue #112, NAS half).

Writes two genuinely distinct copies of an asset to the station's mounted
archive directory (`CIVICCAST_NAS_ARCHIVE_PATH` — a local mount or UNC path;
the operating-system mount is the transport, which covers SMB/NFS appliances
without shipping an rsync or ZFS dependency):

* ``archive/{asset_id}{ext}`` — the canonical copy, overwritten on republish.
* ``snapshots/{asset_id}/{utc-timestamp}-{nonce}{ext}`` — a write-once dated
  copy per publish run.

Every copy is flushed, fsynced, and independently re-read from disk for a
sha256 verification before a proof is minted; a mismatch raises
:class:`LocalNasVerificationError` and no proof is returned. Selected with
``CIVICCAST_PROVIDER_LOCAL_NAS=real``; the deterministic mock remains the
default. The v1.1 rsync/ZFS release-proof gates and the Scott-approved ZFS
deferral ledger are unchanged — this adapter fulfills the two archive
surfaces' copy/snapshot semantics in software where those tools don't exist.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from civiccast.archive.models import ArchiveProof
from civiccast.platform.provider_settings import setting

_CHUNK_BYTES = 1024 * 1024

__all__ = ["LocalNasArchiveClient", "LocalNasSettings", "LocalNasVerificationError"]


class LocalNasVerificationError(RuntimeError):
    """Raised when a written archive copy fails its read-back hash check."""


@dataclass(frozen=True)
class LocalNasSettings:
    """Station NAS archive directory, read from the environment."""

    archive_root: Path

    @classmethod
    def from_env(cls) -> LocalNasSettings:
        raw = setting("local-nas", "archive_path", "CIVICCAST_NAS_ARCHIVE_PATH", "").strip()
        if not raw:
            raise ValueError(
                "CIVICCAST_PROVIDER_LOCAL_NAS=real requires CIVICCAST_NAS_ARCHIVE_PATH "
                "to be set to the mounted archive directory (local mount or UNC path)."
            )
        root = Path(raw).expanduser()
        if not root.exists() or not root.is_dir():
            raise ValueError(
                f"CIVICCAST_NAS_ARCHIVE_PATH={raw!r} is not a reachable directory; "
                "mount the NAS target before selecting the real adapter."
            )
        return cls(archive_root=root)


class LocalNasArchiveClient:
    """Archive client satisfying the same protocol as the mock.

    ``archive`` takes payload bytes (the registry call-site contract);
    ``archive_path`` streams a local media file so full recordings are not
    read into memory. Both return ``(copy_proof, snapshot_proof)`` so the
    publish surfaces keep their existing unpack shape.
    """

    def __init__(self, settings: LocalNasSettings) -> None:
        self._settings = settings

    def archive(self, *, asset_id: str, payload: bytes) -> tuple[ArchiveProof, ArchiveProof]:
        copy_path = self._copy_destination(asset_id, suffix=".bin")
        snapshot_path = self._snapshot_destination(asset_id, suffix=".bin")
        copy_hash = self._write_verified_bytes(copy_path, payload)
        snapshot_hash = self._write_verified_bytes(snapshot_path, payload)
        return (
            self._proof("local_nas_copy", copy_path, copy_hash),
            self._proof("local_nas_snapshot_copy", snapshot_path, snapshot_hash),
        )

    def archive_path(self, *, asset_id: str, path: Path) -> tuple[ArchiveProof, ArchiveProof]:
        if not path.exists() or not path.is_file():
            raise LocalNasVerificationError(f"Archive source media not found: {path}")
        suffix = path.suffix or ".bin"
        copy_path = self._copy_destination(asset_id, suffix=suffix)
        snapshot_path = self._snapshot_destination(asset_id, suffix=suffix)
        copy_hash = self._copy_verified_file(path, copy_path)
        snapshot_hash = self._copy_verified_file(path, snapshot_path)
        return (
            self._proof("local_nas_copy", copy_path, copy_hash),
            self._proof("local_nas_snapshot_copy", snapshot_path, snapshot_hash),
        )

    def _copy_destination(self, asset_id: str, *, suffix: str) -> Path:
        return self._settings.archive_root / "archive" / f"{asset_id}{suffix}"

    def _snapshot_destination(self, asset_id: str, *, suffix: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        nonce = secrets.token_hex(4)
        return self._settings.archive_root / "snapshots" / asset_id / f"{stamp}-{nonce}{suffix}"

    def _write_verified_bytes(self, destination: Path, payload: bytes) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected = hashlib.sha256(payload).hexdigest()
        with destination.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return self._verify(destination, f"sha256:{expected}")

    def _copy_verified_file(self, source: Path, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with source.open("rb") as src, destination.open("wb") as dst:
            for chunk in iter(lambda: src.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        return self._verify(destination, f"sha256:{digest.hexdigest()}")

    def _verify(self, destination: Path, expected_hash: str) -> str:
        observed = self._read_back_hash(destination)
        if observed != expected_hash:
            raise LocalNasVerificationError(
                f"NAS archive copy {destination} failed its read-back hash check "
                f"(expected {expected_hash}, observed {observed}); no proof minted."
            )
        return observed

    def _read_back_hash(self, destination: Path) -> str:
        digest = hashlib.sha256()
        with destination.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _proof(target_type: str, path: Path, verification_hash: str) -> ArchiveProof:
        return ArchiveProof(
            target_type=target_type,  # type: ignore[arg-type]
            target_url_or_path=str(path),
            verification_hash=verification_hash,
            credential_posture="informal_per_station",
        )
