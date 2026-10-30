# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Installer-facing mTLS readiness checks."""

from __future__ import annotations

import os
from pathlib import Path

from civiccast.certs.authority import REQUIRED_SERVICE_IDENTITIES, LocalCertificateAuthority
from civiccast.certs.models import MTLSReadinessSummary, ServiceCertificateStatus


class MTLSReadinessError(RuntimeError):
    """Raised when local CA mTLS readiness cannot be proven."""


def default_cert_root() -> Path:
    configured = os.getenv("CIVICCAST_CERT_ROOT")
    if configured:
        return Path(configured)
    return Path.home() / ".civiccast" / "certs"


def inspect_mtls_readiness(root: Path | str | None = None) -> MTLSReadinessSummary:
    cert_root = Path(root) if root is not None else default_cert_root()
    authority = LocalCertificateAuthority(cert_root)
    certificates: list[ServiceCertificateStatus] = []
    missing: list[str] = []
    rotation_due: list[str] = []
    try:
        authority.inspect_ca()
    except OSError:
        return MTLSReadinessSummary(
            ready=False,
            state="failed",
            message="Local CA certificate is missing for CivicCast internal mTLS.",
            next_step="Run `civiccast cert rotate civiccast-api` to create the local CA and service credentials.",
            required_identities=list(REQUIRED_SERVICE_IDENTITIES),
        )
    for identity in REQUIRED_SERVICE_IDENTITIES:
        try:
            status = authority.inspect_service_certificate(identity)
        except OSError:
            missing.append(identity)
            continue
        certificates.append(status)
        if authority.rotation_status(identity).rotation_due:
            rotation_due.append(identity)
    if missing:
        return MTLSReadinessSummary(
            ready=False,
            state="failed",
            message="Required service certificates are missing: " + ", ".join(missing) + ".",
            next_step=f"Run `civiccast cert rotate {missing[0]}` and repeat for every missing identity.",
            required_identities=list(REQUIRED_SERVICE_IDENTITIES),
            certificates=certificates,
        )
    if rotation_due:
        return MTLSReadinessSummary(
            ready=False,
            state="failed",
            message="Service certificates are expired or inside the rotation window: "
            + ", ".join(rotation_due)
            + ".",
            next_step=f"Run `civiccast cert rotate {rotation_due[0]}` before starting services.",
            required_identities=list(REQUIRED_SERVICE_IDENTITIES),
            certificates=certificates,
        )
    return MTLSReadinessSummary(
        ready=True,
        state="ok",
        message="Local CA and required service certificates are present and valid.",
        next_step="Keep certificate files readable only by the service account and rotate every 90 days.",
        required_identities=list(REQUIRED_SERVICE_IDENTITIES),
        certificates=certificates,
    )


def check_mtls_readiness(root: Path | str | None = None) -> bool:
    summary = inspect_mtls_readiness(root)
    if summary.ready:
        return True
    raise MTLSReadinessError(f"{summary.message} Next: {summary.next_step}")
