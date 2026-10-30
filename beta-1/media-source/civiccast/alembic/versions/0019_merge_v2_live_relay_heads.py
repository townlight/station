# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Merge v2 SQLite manifest and live relay migration heads.

Revision ID: 0019_merge_v2_live_relay_heads
Revises: 0018_manifest_url_nullable, 0010_live_relay_configs
Create Date: 2026-06-01
"""

from __future__ import annotations

revision = "0019_merge_v2_live_relay_heads"
down_revision = ("0018_manifest_url_nullable", "0010_live_relay_configs")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the two completed v2 migration branches."""


def downgrade() -> None:
    """Leave both parent branches available when downgrading through the merge."""
