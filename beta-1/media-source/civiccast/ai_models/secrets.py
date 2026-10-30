# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Keyring-backed AI-provider credentials (S13).

A cloud provider API key (an OpenRouter token, an Ollama Cloud key) is NEVER
stored in the database — the DB holds only an opaque ``credential_ref`` handle.
The real secret lives in the OS keyring under an AI-provider-specific service
name, resolved at call time by the adapter's injected resolver. Mirrors
:mod:`civiccast.auth.keyring_store` and :mod:`civiccast.control_room.secrets`,
but in its own namespace so provider keys, device secrets, and staff tokens never
collide.
"""

from __future__ import annotations

import keyring

from civiccast.ai_models.models import ModelProvider

SERVICE_NAME = "civiccast.ai-provider"

# Stable per-provider credential handles (the opaque ``credential_ref`` stored under
# SERVICE_NAME in the OS keyring). The DB never holds the secret — only the dispatch
# seam resolves the handle to the real key at call time.
_PROVIDER_CREDENTIAL_REF: dict[ModelProvider, str] = {
    "ollama-cloud": "ollama-cloud-key",
    "openrouter": "openrouter-key",
}


def credential_ref_for_provider(provider: ModelProvider) -> str:
    """The keyring handle a cloud/frontier ``provider`` resolves its API key under.

    Local providers (``ollama`` / ``external``) have no credential; asking for one is
    a programming error (the dispatch seam only calls this for cloud/frontier tiers).
    """
    try:
        return _PROVIDER_CREDENTIAL_REF[provider]
    except KeyError as exc:
        raise KeyError(f"Provider {provider!r} has no cloud credential handle.") from exc


class ProviderSecretStoreError(RuntimeError):
    """Raised when the OS credential backend cannot store a provider secret."""


def save_provider_secret(credential_ref: str, secret: str) -> None:
    """Persist a cloud-provider API key under its opaque handle."""

    try:
        keyring.set_password(SERVICE_NAME, credential_ref, secret)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise ProviderSecretStoreError(
            f"Could not save CivicCast AI-provider secret for handle {credential_ref}."
        ) from exc


def load_provider_secret(credential_ref: str) -> str | None:
    """Load a cloud-provider API key by its opaque handle (None if unset)."""

    try:
        return keyring.get_password(SERVICE_NAME, credential_ref)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise ProviderSecretStoreError(
            f"Could not load CivicCast AI-provider secret for handle {credential_ref}."
        ) from exc


def delete_provider_secret(credential_ref: str) -> None:
    """Remove a cloud-provider API key by its opaque handle."""

    try:
        keyring.delete_password(SERVICE_NAME, credential_ref)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise ProviderSecretStoreError(
            f"Could not delete CivicCast AI-provider secret for handle {credential_ref}."
        ) from exc
