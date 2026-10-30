# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Keyring-backed staff token storage helpers."""

from __future__ import annotations

import keyring

SERVICE_NAME = "civiccast.staff-token"


class KeyringStoreError(RuntimeError):
    """Raised when the OS credential backend cannot store a staff token."""


def save_staff_token(operator_id: str, token: str) -> None:
    """Persist the active staff token for an operator in the OS keyring."""

    try:
        keyring.set_password(SERVICE_NAME, operator_id, token)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise KeyringStoreError(
            f"Could not save CivicCast staff token in keyring for {operator_id}."
        ) from exc


def load_staff_token(operator_id: str) -> str | None:
    """Load a staff token from the OS keyring."""

    try:
        return keyring.get_password(SERVICE_NAME, operator_id)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise KeyringStoreError(
            f"Could not load CivicCast staff token from keyring for {operator_id}."
        ) from exc


def delete_staff_token(operator_id: str) -> None:
    """Remove a staff token from the OS keyring."""

    try:
        keyring.delete_password(SERVICE_NAME, operator_id)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise KeyringStoreError(
            f"Could not delete CivicCast staff token from keyring for {operator_id}."
        ) from exc
