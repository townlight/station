# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""OTT app build-record + store-submission entities (S12 / build step 8).

Net-new auxiliary entities over the shipped app-platform contract
(:mod:`civiccast.app_platform.models`). ``AppBuildRecord`` is an immutable log
entry for one locally-produced platform build (artifact + SHA-256, verified
locally); ``StoreSubmissionMetadata`` tracks an external store submission's
status (the operator updates it by hand — CivicCast makes no calls to app
stores). No schema migration: these persist in a file-backed auxiliary store
(:mod:`civiccast.app_platform.build_store`), mirroring AppPlatformConfigStore.

Proof boundary: a build record claims only "local build artifact, SHA-256
verified" — never app-store certification (that is external + human-gated).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from civiccast.app_platform.models import AppBuildTier, AppTarget

SubmissionStatus = Literal[
    "draft", "pending_review", "approved", "rejected", "published", "withdrawn"
]

__all__ = ["AppBuildRecord", "StoreSubmissionMetadata", "SubmissionStatus"]


class StoreSubmissionMetadata(BaseModel):
    """External store submission metadata + status (operator-maintained)."""

    model_config = ConfigDict(extra="forbid")

    app_target: AppTarget
    store_account_email: Annotated[str | None, Field(default=None, max_length=200)] = None
    package_id: Annotated[str | None, Field(default=None, max_length=200)] = None
    version_code: Annotated[int, Field(ge=0)] = 1
    version_name: Annotated[str, Field(min_length=1, max_length=40)] = "0.1.0"
    submitted_at: datetime | None = None
    submission_status: SubmissionStatus = "draft"
    submission_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    published_url: Annotated[str | None, Field(default=None, max_length=500)] = None
    support_contact: Annotated[str | None, Field(default=None, max_length=200)] = None
    # Audit trail for operator-maintained status transitions (set by the router
    # from the verified token identity; see build_router.update_store_submission).
    updated_by: Annotated[str | None, Field(default=None, max_length=120)] = None
    updated_at: datetime | None = None


class AppBuildRecord(BaseModel):
    """Immutable record of a locally-produced platform-specific app build."""

    model_config = ConfigDict(extra="forbid")

    record_id: Annotated[str, Field(min_length=1, max_length=120)]
    station_id: Annotated[str, Field(min_length=1, max_length=120)]
    app_target: AppTarget
    build_tier: AppBuildTier
    app_name: Annotated[str, Field(min_length=1, max_length=160)]
    icon_url: Annotated[str | None, Field(default=None, max_length=500)] = None
    splash_url: Annotated[str | None, Field(default=None, max_length=500)] = None
    channels: list[dict[str, Any]] = Field(default_factory=list)
    artifact_path: Annotated[str, Field(min_length=1, max_length=500)]
    artifact_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    entry_point: Annotated[str, Field(min_length=1, max_length=500)]
    manifest_json: dict[str, Any] = Field(default_factory=dict)
    built_at: datetime
    built_by: Annotated[str, Field(min_length=1, max_length=120)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=160)] = (
        "local-build-artifact-sha256-verified"
    )
    store_submission: StoreSubmissionMetadata | None = None

    @field_validator("artifact_sha256")
    @classmethod
    def _sha256_is_lowercase_hex(cls, value: str) -> str:
        candidate = value.strip().lower()
        if len(candidate) != 64 or any(c not in "0123456789abcdef" for c in candidate):
            raise ValueError("artifact_sha256 must be 64 lowercase hex characters")
        return candidate
