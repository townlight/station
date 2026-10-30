# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic archive adapter models for v0.7."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ArchiveProof(BaseModel):
    """Result of an archive write plus hash verification."""

    model_config = ConfigDict(extra="forbid")

    target_type: Literal[
        "internet_archive",
        "local_nas_rsync",
        "local_nas_zfs",
        "local_nas_copy",
        "local_nas_snapshot_copy",
    ]
    target_url_or_path: Annotated[str, Field(min_length=1)]
    verification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    credential_posture: Literal["informal_per_station"]
    # GauntletGate TW-1: a proof produced by a mock provider carried nothing to
    # distinguish it from a real archival write, so the publish dashboard showed
    # a plausible archive.org link for a meeting that was never archived.
    # Defaults False, so real clients are unaffected.
    simulated: bool = False


class LocalNasArchivePlan(BaseModel):
    """The local-NAS archive plan covers both data-copy modes."""

    model_config = ConfigDict(extra="forbid")

    rsync_destination: Annotated[str, Field(min_length=1)]
    zfs_send_command: Annotated[str, Field(min_length=1)]
    verification_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


# RFC 2606 reserves .invalid so it can never resolve. A simulated archive
# target must be impossible to mistake for a real permalink, even when it is
# copied out of an API response with no surrounding context.
SIMULATED_IA_BASE = "https://internet-archive.simulated.invalid/details"


def _digest(asset_id: str, payload: bytes) -> str:
    h = hashlib.sha256()
    h.update(asset_id.encode("utf-8"))
    h.update(b"\0")
    h.update(payload)
    return f"sha256:{h.hexdigest()}"


class MockInternetArchiveClient:
    """Deterministic IA client for CI and local proof.

    Emits a target on the RFC 2606 ``.invalid`` TLD, which can never resolve, and
    flags the proof ``simulated``. Both are deliberate: this client is the default
    provider until an admin sets ``CIVICCAST_PROVIDER_INTERNET_ARCHIVE=real``, and
    it previously returned a real-looking ``archive.org/details/<id>`` permalink
    for an item it never created (GauntletGate TW-1).
    """

    def upload(self, *, asset_id: str, payload: bytes) -> ArchiveProof:
        return ArchiveProof(
            target_type="internet_archive",
            target_url_or_path=f"{SIMULATED_IA_BASE}/{asset_id}",
            verification_hash=_digest(asset_id, payload),
            credential_posture="informal_per_station",
            simulated=True,
        )


class MockLocalNasArchiveClient:
    """Deterministic local-NAS client proving both rsync and ZFS paths."""

    def archive(self, *, asset_id: str, payload: bytes) -> tuple[ArchiveProof, ArchiveProof]:
        digest = _digest(asset_id, payload)
        rsync = ArchiveProof(
            target_type="local_nas_rsync",
            target_url_or_path=f"/nas/civiccast/archive/{asset_id}.mp4",
            verification_hash=digest,
            credential_posture="informal_per_station",
            simulated=True,
        )
        zfs = ArchiveProof(
            target_type="local_nas_zfs",
            target_url_or_path=f"zfs://civiccast/archive@{asset_id}",
            verification_hash=digest,
            credential_posture="informal_per_station",
            simulated=True,
        )
        return rsync, zfs
