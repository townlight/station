# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Configuration for the local ActivityPub federation surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

FederationMode = Literal["open", "limited", "approval-only", "disabled"]


@dataclass(frozen=True)
class ActivityPubConfig:
    """Operator-owned ActivityPub posture for one CivicCast app instance."""

    handle: str = "civiccast"
    display_name: str = "CivicCast"
    federation_mode: FederationMode = "disabled"
    base_url: str = ""
    private_key_path: str = ""
    blocked_instances: frozenset[str] = frozenset()
    allowed_instances: frozenset[str] = frozenset()
    authorized_fetch: bool = False
    lab_allow_local: bool = False
    inbox_rate_limit: int = 60
    inbox_rate_window_seconds: int = 60
    public_key_pem: str = ""


def _split_domains(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _load_blocklist_from_file(path: str) -> set[str]:
    blocklist_path = Path(path)
    if not blocklist_path.exists():
        return set()
    domains: set[str] = set()
    for line in blocklist_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            domains.add(stripped.lower())
    return domains


def load_activitypub_config(env: dict[str, str] | None = None) -> ActivityPubConfig:
    """Load ActivityPub configuration from environment-style key/value pairs."""

    source = env or os.environ
    mode = source.get("CIVICCAST_ACTIVITYPUB_MODE", "disabled").strip().lower()
    if mode not in {"open", "limited", "approval-only", "disabled"}:
        mode = "disabled"
    base_url = _normalize_base_url(
        source.get("CIVICCAST_ACTIVITYPUB_BASE_URL")
        or source.get("CIVICCAST_PUBLIC_BASE_URL")
        or ""
    )
    private_key_path = source.get("CIVICCAST_ACTIVITYPUB_PRIVATE_KEY_PATH", "").strip()
    public_key_pem = source.get("CIVICCAST_ACTIVITYPUB_PUBLIC_KEY_PEM", "").strip()
    if mode != "disabled":
        if private_key_path:
            try:
                from civiccast.activitypub.keys import public_key_pem_from_private_key_path

                public_key_pem = public_key_pem_from_private_key_path(Path(private_key_path))
            except (OSError, ValueError):
                mode = "disabled"
        if not base_url or not public_key_pem or not private_key_path:
            mode = "disabled"

    blocked = _split_domains(source.get("CIVICCAST_ACTIVITYPUB_BLOCKLIST", ""))
    blocklist_file = source.get("CIVICCAST_ACTIVITYPUB_BLOCKLIST_FILE", "")
    if blocklist_file:
        blocked.update(_load_blocklist_from_file(blocklist_file))
    allowed = _split_domains(source.get("CIVICCAST_ACTIVITYPUB_ALLOWLIST", ""))
    allowlist_file = source.get("CIVICCAST_ACTIVITYPUB_ALLOWLIST_FILE", "")
    if allowlist_file:
        allowed.update(_load_blocklist_from_file(allowlist_file))

    return ActivityPubConfig(
        handle=source.get("CIVICCAST_ACTIVITYPUB_HANDLE", "civiccast").strip() or "civiccast",
        display_name=source.get("CIVICCAST_ACTIVITYPUB_DISPLAY_NAME", "CivicCast").strip()
        or "CivicCast",
        federation_mode=mode,  # type: ignore[arg-type]
        base_url=base_url,
        private_key_path=private_key_path,
        blocked_instances=frozenset(blocked),
        allowed_instances=frozenset(allowed),
        authorized_fetch=_truthy(source.get("CIVICCAST_ACTIVITYPUB_AUTHORIZED_FETCH", "")),
        lab_allow_local=_truthy(source.get("CIVICCAST_ACTIVITYPUB_LAB_ALLOW_LOCAL", "")),
        inbox_rate_limit=max(1, int(source.get("CIVICCAST_ACTIVITYPUB_INBOX_RATE_LIMIT", "60"))),
        inbox_rate_window_seconds=max(
            1, int(source.get("CIVICCAST_ACTIVITYPUB_INBOX_RATE_WINDOW_SECONDS", "60"))
        ),
        public_key_pem=public_key_pem,
    )


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_base_url(value: str) -> str:
    stripped = value.strip().rstrip("/")
    if not stripped:
        return ""
    parsed = urlparse(stripped)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
        "testserver",
    }:
        return ""
    return stripped
