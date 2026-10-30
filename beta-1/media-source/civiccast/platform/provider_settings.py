# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Read protected Setup credentials without copying secrets into process env."""

from __future__ import annotations

import os

PROVIDER_SETUP_IDS = {
    "internet_archive": "internet-archive",
    "youtube": "youtube",
    "local_nas": "local-nas",
    "mail": "subscriber-notifications",
}


def saved_fields(provider_id: str) -> dict[str, str]:
    # Lazy import: installer readiness itself calls the adapter settings loaders.
    from civiccast.installer.service import stored_provider_field_values

    return stored_provider_field_values(provider_id)


def setting(provider_id: str, field: str, env_name: str, default: str = "") -> str:
    """Saved Setup values take precedence; retain environment-only deployments."""
    return saved_fields(provider_id).get(field, os.environ.get(env_name, default)).strip()
