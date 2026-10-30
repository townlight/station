# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Shared HMAC-SHA-256 payload signer used by both the subscriber stack and S8.

OD-7: both stacks share the same crypto primitive; secrets are kept in their
own separate credential stores so the routing layers stay fully decoupled.
"""

from __future__ import annotations

import hashlib
import hmac
import json


def sign_payload(payload: dict[str, object], secret: str) -> str:
    """Return hex HMAC-SHA-256 of *payload* (canonical JSON, sorted keys)."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def sign_body(body: bytes, secret: str) -> str:
    """Return hex HMAC-SHA-256 of raw *body* bytes (for pre-serialised payloads)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
