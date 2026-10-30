# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Trust wiring for the native server-binaries component pack (PostgreSQL 17
binaries).

NATS JetStream was removed from the product entirely (owner decision
2026-08-20, ADR 0023 "NATS removed -- in-process event bus", which
supersedes ADR 0001); the ``native-server-binaries`` pack this module
verifies no longer carries ``nats-server.exe`` (that packaging change lives
outside this module's scope -- see :mod:`civiccast.installer.native_packs`).

Delegates entirely to :func:`civiccast.installer.native_packs.verify_native_pack`
-- the SAME signature + byte-inventory verification every other native
component pack goes through -- so this module never re-implements trust
logic (WS5 task instruction: "follow native_packs.py conventions"). The
provisioning orchestrator's FIRST forward phase
(``ProvisionPhase.PACK_VERIFIED``) calls this before any provisioning action
touches disk: initdb never runs, no config file is written, and no directory
is created against an unverified pack.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from civiccast.installer.native_packs import NativePackResult, verify_native_pack

#: The pack "component" identity for the PostgreSQL 17 server-binaries pack,
#: matching the ``component`` field ``native_packs`` manifests carry.
SERVER_BINARIES_COMPONENT = "native-server-binaries"


def verify_server_binaries_pack(
    pack_path: str | Path,
    *,
    public_key: Ed25519PublicKey,
    expected_product_version: str,
    expected_compatible_core: str,
    expected_signing_key_id: str,
) -> NativePackResult:
    """Verify the hash-pinned, signed PostgreSQL server-binaries pack.

    Raises :class:`civiccast.installer.native_packs.NativePackVerificationError`
    on any signature, identity, or byte-inventory mismatch -- fail-closed,
    never a partial/best-effort accept.
    """

    return verify_native_pack(
        Path(pack_path),
        public_key=public_key,
        expected_component=SERVER_BINARIES_COMPONENT,
        expected_product_version=expected_product_version,
        expected_compatible_core=expected_compatible_core,
        expected_signing_key_id=expected_signing_key_id,
    )


__all__ = ["SERVER_BINARIES_COMPONENT", "verify_server_binaries_pack"]
