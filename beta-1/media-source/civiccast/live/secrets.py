# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Keyring-backed live-source credentials (WP-07).

``live_sources.credentials_handle`` has existed since migration
``0007_live_sessions`` and, until this module, nothing anywhere in the product
read it back. ``civiccast.live.source_probe``'s docstring said so in as many
words: "No credential-resolution helper exists anywhere in this codebase yet
... nothing reads ``credentials_handle`` back out." An operator could type a
handle into Setup, the API would echo it back, and the station would then
probe and open the source with no credential at all.

This closes that. The pattern is the one the repository already uses for every
other secret it holds -- :mod:`civiccast.auth.keyring_store`,
:mod:`civiccast.control_room.secrets`, :mod:`civiccast.ai_models.secrets`: the
database stores only the opaque handle, the real secret lives in the OS
credential store under a namespace of its own, and it is resolved **at
execution time** by an injected :data:`SecretResolver` -- never at
configuration time, never at graph-build time, never into a row.

Only SRT carries a credential (see
``civiccast.live.source_endpoints.CREDENTIAL_SUPPORTED_SOURCE_TYPES`` for why
RTSP, RTMP, and NDI cannot). Its single secret value is the SRT passphrase.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import keyring

__all__ = [
    "SERVICE_NAME",
    "LiveSourceSecretStoreError",
    "SecretResolver",
    "delete_live_source_secret",
    "load_live_source_secret",
    "redact_secrets",
    "save_live_source_secret",
]

#: Own keyring namespace so a live-source passphrase can never collide with a
#: staff token, a control-room device secret, or an AI-provider key.
SERVICE_NAME = "civiccast.live-source"

#: Resolve an opaque handle to its secret, or ``None`` when unset. Matches
#: ``civiccast.ai_models.cloud.ollama_cloud.SecretResolver`` and the resolver
#: ``civiccast.egress.gst.bridge`` already threads through for SRT sinks.
SecretResolver = Callable[[str], "str | None"]


class LiveSourceSecretStoreError(RuntimeError):
    """Raised when the OS credential backend cannot service a live-source secret."""


def save_live_source_secret(credentials_handle: str, secret: str) -> None:
    """Persist an SRT passphrase under its opaque handle."""

    try:
        keyring.set_password(SERVICE_NAME, credentials_handle, secret)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise LiveSourceSecretStoreError(
            f"Could not save the CivicCast live-source secret for handle {credentials_handle}."
        ) from exc


def load_live_source_secret(credentials_handle: str) -> str | None:
    """Load an SRT passphrase by its opaque handle (``None`` if unset)."""

    try:
        return keyring.get_password(SERVICE_NAME, credentials_handle)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise LiveSourceSecretStoreError(
            f"Could not read the CivicCast live-source secret for handle {credentials_handle}."
        ) from exc


def delete_live_source_secret(credentials_handle: str) -> None:
    """Remove an SRT passphrase by its opaque handle."""

    try:
        keyring.delete_password(SERVICE_NAME, credentials_handle)
    except Exception as exc:  # pragma: no cover - backend-specific
        raise LiveSourceSecretStoreError(
            f"Could not delete the CivicCast live-source secret for handle {credentials_handle}."
        ) from exc


def redact_secrets(text: str, secrets: str | Iterable[str] | None) -> str:
    """Replace every occurrence of a resolved secret in ``text`` with ``[redacted]``.

    Belt-and-suspenders. CivicCast never puts a passphrase into an address or a
    log line, but ``text`` here is usually a *third party's* output -- ffprobe's
    stderr -- and a future libsrt build echoing back the option it was handed
    must not be the way a passphrase reaches an operator-visible probe detail,
    a proof file, or the CHANGELOG of a support ticket.

    ``secrets`` is one resolved value (a bare string) or several (any
    iterable of strings, including the ``tuple[str, ...]`` the probe's own
    credential resolver returns) -- both call shapes are real callers, not
    hypothetical, so this takes the union rather than a loosely-typed
    ``object`` and a runtime cast.

    Short values are skipped: replacing every occurrence of a two-character
    "secret" would corrupt an otherwise useful diagnostic message without
    protecting anything meaningful.
    """
    if not text or not secrets:
        return text
    values: Iterable[str] = (secrets,) if isinstance(secrets, str) else secrets
    redacted = text
    for value in values:
        if isinstance(value, str) and len(value) >= 4:
            redacted = redacted.replace(value, "[redacted]")
    return redacted
