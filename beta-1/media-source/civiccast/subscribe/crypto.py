# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic local crypto helpers for v0.8 subscription proofs."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Protocol

from civiccast.common.webhook_sign import sign_payload as sign_payload


class SecretBox(Protocol):
    def seal(self, plaintext: str, *, aad: str) -> str: ...
    def open(self, token: str, *, aad: str) -> str: ...


class DeterministicSecretBox:
    """Small deterministic envelope used for local/CI proof.

    This is deliberately a replaceable abstraction. v0.8 needs to prove that
    subscriber handles and webhook secrets are never stored as plaintext, while
    avoiding real OS keychain secrets in CI. The token is authenticated with
    HMAC and obfuscated with a SHA-256 keystream.
    """

    def __init__(self, key: str) -> None:
        self._key = key.encode()

    def seal(self, plaintext: str, *, aad: str) -> str:
        nonce = hashlib.sha256(f"{aad}:{plaintext}".encode()).digest()[:12]
        stream = self._stream(nonce, aad, len(plaintext.encode()))
        cipher = bytes(a ^ b for a, b in zip(plaintext.encode(), stream, strict=True))
        mac = hmac.new(self._key, nonce + aad.encode() + cipher, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + mac + cipher).decode().rstrip("=")

    def open(self, token: str, *, aad: str) -> str:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        nonce, mac, cipher = raw[:12], raw[12:44], raw[44:]
        expected = hmac.new(self._key, nonce + aad.encode() + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError("encrypted subscription value failed authentication")
        stream = self._stream(nonce, aad, len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream, strict=True)).decode()

    def _stream(self, nonce: bytes, aad: str, size: int) -> bytes:
        output = bytearray()
        counter = 0
        while len(output) < size:
            output.extend(
                hmac.new(
                    self._key,
                    nonce + aad.encode() + counter.to_bytes(4, "big"),
                    hashlib.sha256,
                ).digest()
            )
            counter += 1
        return bytes(output[:size])


def signed_token(payload: dict[str, object], secret: str) -> str:
    data = dict(payload)
    data.setdefault("issued_at", datetime.now(UTC).isoformat())
    body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + sig


def require_random_webhook_secret(subscription_id: str, secret: str) -> None:
    """Older identifier-derived keys were public; require endpoint re-registration."""
    legacy = hashlib.sha256(f"{subscription_id}:webhook".encode()).hexdigest()
    if hmac.compare_digest(secret, legacy):
        raise ValueError("Webhook needs re-registration and endpoint confirmation before delivery.")


def verify_token(token: str, secret: str) -> dict[str, object]:
    try:
        body_b64, sig = token.split(".", 1)
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
    except ValueError as exc:
        raise ValueError("Subscription link is malformed. Request a new signup link.") from exc
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("Subscription link is invalid or has been changed. Request a new link.")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("Subscription link payload is invalid. Request a new link.")
    return parsed
