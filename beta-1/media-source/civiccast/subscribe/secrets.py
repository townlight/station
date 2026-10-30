# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deployment secret loading and rotation helpers for subscriptions.

The durable local secret file is protected before bytes are written. Existing
files with broad access are atomically replaced with the same bytes; an ACL or
replacement failure is surfaced without generating replacement keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as secrets_lib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from civiccast.native.private_files import (
    private_file_lock,
    read_private_bytes,
    write_private_bytes_atomic,
)
from civiccast.subscribe.crypto import DeterministicSecretBox, SecretBox, verify_token

_MIN_SECRET_LENGTH = 32
_SECRET_FILE_VERSION = 1


class SubscriptionSecretConfigurationError(RuntimeError):
    """Raised when subscription secrets are missing or malformed."""


class AesGcmSecretBox:
    """AES-GCM envelope for new subscription handles and webhook secrets."""

    def __init__(self, key_material: str) -> None:
        self._key = hashlib.sha256(key_material.encode("utf-8")).digest()

    def seal(self, plaintext: str, *, aad: str) -> str:
        nonce = secrets_lib.token_bytes(12)
        cipher = AESGCM(self._key).encrypt(nonce, plaintext.encode("utf-8"), aad.encode("utf-8"))
        return base64.urlsafe_b64encode(nonce + cipher).decode("ascii").rstrip("=")

    def open(self, token: str, *, aad: str) -> str:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        nonce, cipher = raw[:12], raw[12:]
        try:
            return AESGCM(self._key).decrypt(nonce, cipher, aad.encode("utf-8")).decode("utf-8")
        except InvalidTag as exc:
            raise ValueError("encrypted subscription value failed authentication") from exc


class RotatingSecretBox:
    """Open with primary or legacy boxes; seal only with the primary box."""

    def __init__(self, primary: SecretBox, legacy: tuple[SecretBox, ...] = ()) -> None:
        self._primary = primary
        self._legacy = legacy

    def seal(self, plaintext: str, *, aad: str) -> str:
        return self._primary.seal(plaintext, aad=aad)

    def open(self, token: str, *, aad: str) -> str:
        failures: list[Exception] = []
        for box in (self._primary, *self._legacy):
            try:
                return box.open(token, aad=aad)
            except ValueError as exc:
                failures.append(exc)
        raise ValueError("encrypted subscription value failed authentication") from failures[-1]


@dataclass(frozen=True)
class SubscriptionSecrets:
    """Current subscription signing/encryption material plus legacy readers."""

    token_secret: str
    legacy_token_secrets: tuple[str, ...]
    secret_box: SecretBox
    source: str


def load_subscription_secrets(
    source: Mapping[str, str] | None = None,
) -> SubscriptionSecrets:
    """Load configured secrets or create/migrate the durable local secret file."""

    env = source or os.environ
    if env.get("CIVICCAST_ALLOW_DETERMINISTIC_SUBSCRIBE_SECRETS") == "1":
        return SubscriptionSecrets(
            token_secret=_legacy_v08_token_secret(),
            legacy_token_secrets=(),
            secret_box=DeterministicSecretBox(_legacy_v08_box_key()),
            source="deterministic-test-opt-in",
        )

    token_secret = env.get("CIVICCAST_SUBSCRIBE_TOKEN_SECRET", "").strip()
    encryption_key = env.get("CIVICCAST_SUBSCRIBE_ENCRYPTION_KEY", "").strip()
    if token_secret or encryption_key:
        if not token_secret or not encryption_key:
            raise SubscriptionSecretConfigurationError(
                "Set both CIVICCAST_SUBSCRIBE_TOKEN_SECRET and "
                "CIVICCAST_SUBSCRIBE_ENCRYPTION_KEY, or set neither and let "
                "CivicCast create a durable local secrets file."
            )
        return _build_secrets(
            token_secret=token_secret,
            encryption_key=encryption_key,
            source="environment",
            env=env,
        )

    secret_path = _secret_file_path(env)
    with private_file_lock(secret_path):
        if secret_path.exists():
            payload = json.loads(read_private_bytes(secret_path).decode("utf-8"))
            return _build_secrets(
                token_secret=str(payload.get("token_secret", "")),
                encryption_key=str(payload.get("encryption_key", "")),
                source=str(secret_path),
                env=env,
                legacy_token_secrets=tuple(str(v) for v in payload.get("legacy_token_secrets", [])),
                legacy_encryption_keys=tuple(
                    str(v) for v in payload.get("legacy_encryption_keys", [])
                ),
            )

        payload = {
            "format_version": _SECRET_FILE_VERSION,
            "token_secret": secrets_lib.token_urlsafe(48),
            "encryption_key": secrets_lib.token_urlsafe(48),
            "legacy_token_secrets": [],
            "legacy_encryption_keys": [],
        }
        serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        write_private_bytes_atomic(secret_path, serialized)
        return _build_secrets(
            token_secret=payload["token_secret"],
            encryption_key=payload["encryption_key"],
            source=str(secret_path),
            env=env,
        )


def verify_subscription_token(token: str, secrets: SubscriptionSecrets) -> dict[str, object]:
    """Verify a subscription token against current then legacy signing secrets."""

    last_error: ValueError | None = None
    for token_secret in (secrets.token_secret, *secrets.legacy_token_secrets):
        try:
            return verify_token(token, token_secret)
        except ValueError as exc:
            last_error = exc
    if last_error is None:
        raise ValueError("Subscription link is invalid. Request a new link.")
    raise last_error


def _build_secrets(
    *,
    token_secret: str,
    encryption_key: str,
    source: str,
    env: Mapping[str, str],
    legacy_token_secrets: tuple[str, ...] = (),
    legacy_encryption_keys: tuple[str, ...] = (),
) -> SubscriptionSecrets:
    _validate_secret("subscription token secret", token_secret)
    _validate_secret("subscription encryption key", encryption_key)
    env_legacy_tokens = _split_secret_list(env.get("CIVICCAST_SUBSCRIBE_LEGACY_TOKEN_SECRETS", ""))
    env_legacy_keys = _split_secret_list(env.get("CIVICCAST_SUBSCRIBE_LEGACY_ENCRYPTION_KEYS", ""))
    legacy_tokens = (*legacy_token_secrets, *env_legacy_tokens)
    legacy_keys = (*legacy_encryption_keys, *env_legacy_keys)
    legacy_boxes: tuple[SecretBox, ...] = tuple(AesGcmSecretBox(key) for key in legacy_keys)
    if env.get("CIVICCAST_SUBSCRIBE_ACCEPT_V08_LEGACY_SECRETS") == "1":
        legacy_tokens = (*legacy_tokens, _legacy_v08_token_secret())
        legacy_boxes = (*legacy_boxes, DeterministicSecretBox(_legacy_v08_box_key()))
    return SubscriptionSecrets(
        token_secret=token_secret,
        legacy_token_secrets=tuple(_dedupe(legacy_tokens)),
        secret_box=RotatingSecretBox(AesGcmSecretBox(encryption_key), legacy_boxes),
        source=source,
    )


def _secret_file_path(env: Mapping[str, str]) -> Path:
    configured = env.get("CIVICCAST_SUBSCRIBE_SECRETS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    config_dir = env.get("CIVICCAST_CONFIG_DIR", "").strip()
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".civiccast"
    return base / "subscribe-secrets.json"


def _split_secret_list(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(";") if item.strip())


def _validate_secret(label: str, value: str) -> None:
    if len(value) < _MIN_SECRET_LENGTH:
        raise SubscriptionSecretConfigurationError(
            f"{label} must be at least {_MIN_SECRET_LENGTH} characters."
        )


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            ordered.append(value)
            seen.add(value)
    return tuple(ordered)


def _legacy_v08_token_secret() -> str:
    return "civiccast-v08-" + "subscription-token-secret"


def _legacy_v08_box_key() -> str:
    return "civiccast-v08-" + "local-proof-key"
