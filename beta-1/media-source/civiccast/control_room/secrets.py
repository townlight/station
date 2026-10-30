# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Keyring-backed production-device secrets (S16).

A ``ProductionDevice`` never stores a device credential (obs-websocket
password, vMix auth, an ATEM key) in the database — only an opaque
``secret_ref`` handle. The actual secret lives in the OS keyring under a
control-room-specific service name, resolved at fire time by the Node TSR
sidecar's CivicCast-side caller. Mirrors :mod:`civiccast.auth.keyring_store`
but in its own namespace so device secrets and staff tokens never collide.
"""

from __future__ import annotations

import keyring

SERVICE_NAME = "civiccast.control-room-device"


class DeviceSecretStoreError(RuntimeError):
    """Raised when the OS credential backend cannot store a device secret."""


def save_device_secret(secret_ref: str, secret: str) -> None:
    """Persist a device control secret under its opaque handle."""

    try:
        keyring.set_password(SERVICE_NAME, secret_ref, secret)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise DeviceSecretStoreError(
            f"Could not save CivicCast device secret for handle {secret_ref}."
        ) from exc


def load_device_secret(secret_ref: str) -> str | None:
    """Load a device control secret by its opaque handle."""

    try:
        return keyring.get_password(SERVICE_NAME, secret_ref)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise DeviceSecretStoreError(
            f"Could not load CivicCast device secret for handle {secret_ref}."
        ) from exc


def delete_device_secret(secret_ref: str) -> None:
    """Remove a device control secret by its opaque handle."""

    try:
        keyring.delete_password(SERVICE_NAME, secret_ref)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise DeviceSecretStoreError(
            f"Could not delete CivicCast device secret for handle {secret_ref}."
        ) from exc
