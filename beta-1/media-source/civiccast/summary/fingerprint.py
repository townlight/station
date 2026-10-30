# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic audit fingerprints for approved summary records."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sha256_fingerprint(payload: dict[str, Any]) -> str:
    """Return a stable sha256 fingerprint over JSON-serializable data."""

    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()
