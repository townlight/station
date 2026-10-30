# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Egress schema-currency surface (S9 §4.4 / §6.6).

An operator watching System Health must be able to see whether the running code
matches the persisted egress schema — a skew can silently corrupt data. Each health
sample stamps ``EGRESS_SCHEMA_VERSION``; the operator UI flags a mismatch.

Bump ``EGRESS_SCHEMA_VERSION`` on any BREAKING egress entity change (a new required
field, a removed field, an enum rename). Additive nullable columns do not require a
bump.
"""

from __future__ import annotations

from typing import Protocol

EGRESS_SCHEMA_VERSION = 1


def current_schema_version() -> int:
    """The egress schema version this running code expects."""
    return EGRESS_SCHEMA_VERSION


class _HasSchemaVersion(Protocol):
    schema_version: int


def is_schema_current(sample: _HasSchemaVersion) -> bool:
    """True if ``sample`` was written by code at the current schema version."""
    return sample.schema_version == current_schema_version()
