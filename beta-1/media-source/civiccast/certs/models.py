# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Private-key-safe public certificate status models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class CertificateAuthorityStatus(BaseModel):
    """Inspectable CA metadata that never serializes private key material."""

    model_config = ConfigDict(extra="forbid")

    common_name: Annotated[str, Field(min_length=1)]
    ca_certificate_path: Path
    fingerprint_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    not_before: datetime
    not_after: datetime

    @property
    def private_key_path(self) -> None:
        return None


class ServiceCertificateStatus(BaseModel):
    """Inspectable service certificate metadata safe for CLI/API output."""

    model_config = ConfigDict(extra="forbid")

    service_identity: Annotated[str, Field(min_length=1)]
    certificate_path: Path
    fingerprint_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    issuer_fingerprint_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    subject_alternative_names: list[str]
    not_before: datetime
    not_after: datetime
    retired_certificate_fingerprint_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @property
    def private_key_path(self) -> None:
        return None


class CertificateRotationStatus(BaseModel):
    """Rotation health for one service identity."""

    model_config = ConfigDict(extra="forbid")

    service_identity: Annotated[str, Field(min_length=1)]
    state: Literal["healthy", "rotation_due", "missing", "expired"]
    rotation_due: bool
    expires_at: datetime | None = None
    next_step: Annotated[str, Field(min_length=1)]


class MTLSReadinessSummary(BaseModel):
    """Aggregate mTLS readiness without key paths or key bytes."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    state: Literal["ok", "failed", "error"]
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]
    required_identities: list[str]
    certificates: list[ServiceCertificateStatus] = Field(default_factory=list)
